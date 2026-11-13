import { showFeedback, confirmAction } from './FeedbackDialog';
import { For, Show, createMemo, createSignal, on, onCleanup, createEffect } from 'solid-js';
import { Portal } from 'solid-js/web';
import {
  batchDeleteSymphonyLocalIssues,
  deleteSymphonyLocalIssue,
  getSubagents,
  getSymphonyLocalIssues,
  updateSymphonyLocalIssue,
  type SymphonyLocalIssue,
} from '../api/client';
import { sessionStore } from '../stores/session';
import { createPageVisibleSignal } from '../utils/pageVisibility';
import { formatAsyncResultSummary } from '../utils/asyncResultSummary';
import { issueListSignature } from '../utils/symphonyIssueSnapshot';
import { hasAcceptedSubmission, isDispatchingSubmission, isUnresolvedSubmission } from '../utils/unresolvedSubmission';
import HumanReviewPanel from './HumanReviewPanel';
import JobWorkspacePanel from './JobWorkspacePanel';
import WorkflowWorkspacePanel from './WorkflowWorkspacePanel';

const ANNOUNCED_ISSUES_STORAGE_KEY = 'tcode_symphony_announced_issue_keys';
const ISSUES_POLL_INTERVAL_MS = 8000;
const ACTIVE_ISSUES_POLL_INTERVAL_MS = 3000;
const COMPLETION_NOTICE_TTL_MS = 12000;
const BATCH_DELETE_LIMIT = 100;

type IssueFilterMode = 'all' | 'session' | 'workspace';
type IssueKanbanStatus = 'todo' | 'in_progress' | 'done' | 'failed';
type CompletionNotice = {
  key: string;
  title: string;
  summary: string;
  state: string;
};

const ISSUE_KANBAN_COLUMNS: Array<{
  status: IssueKanbanStatus;
  label: string;
  description: string;
  accent: string;
  dot: string;
}> = [
  {
    status: 'todo',
    label: 'Todo',
    description: '等待领取或审核',
    accent: 'text-[#ffe1b3]',
    dot: 'bg-[#f1a02b]',
  },
  {
    status: 'in_progress',
    label: 'In Progress',
    description: '正在执行',
    accent: 'text-[#8ed5ff]',
    dot: 'bg-[#38bdf8]',
  },
  {
    status: 'done',
    label: 'Done',
    description: '已完成',
    accent: 'text-[#9ee6bb]',
    dot: 'bg-[#38b46a]',
  },
  {
    status: 'failed',
    label: 'Failed',
    description: '失败或取消',
    accent: 'text-[#ffb6b0]',
    dot: 'bg-[#da3633]',
  },
];

function loadAnnouncedIssueKeys(): Set<string> {
  try {
    const raw = window.localStorage.getItem(ANNOUNCED_ISSUES_STORAGE_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.map((item) => String(item || '').trim()).filter(Boolean));
  } catch {
    return new Set();
  }
}

function saveAnnouncedIssueKeys(keys: Set<string>) {
  try {
    window.localStorage.setItem(
      ANNOUNCED_ISSUES_STORAGE_KEY,
      JSON.stringify(Array.from(keys.values()))
    );
  } catch {
    // Ignore localStorage failures.
  }
}

export default function SymphonyWorkspace(props: {active?: boolean; jobsOnly?: boolean; focusRequest?: {id: string; key: number}} = {}) {
  const [issues, setIssues] = createSignal<SymphonyLocalIssue[]>([]);
  const [issueSnapshot, setIssueSnapshot] = createSignal('');
  const [total, setTotal] = createSignal(0);
  const [loading, setLoading] = createSignal(false);
  const [expandedIssue, setExpandedIssue] = createSignal<string | null>(null);
  const [selectedIssueModal, setSelectedIssueModal] = createSignal<SymphonyLocalIssue | null>(null);
  createEffect(on(() => props.focusRequest, request => {
    if (!request) {setSelectedIssueModal(null);return;}
    const workspace = currentWorkspaceId();
    void getSymphonyLocalIssues(10, '', {workspaceId: workspace, query: request.id}).then(data => {
      if (props.focusRequest?.key !== request.key || currentWorkspaceId() !== workspace) return;
      const issue = data.issues.find(item => item.id === request.id || item.identifier === request.id);
      if (issue) setSelectedIssueModal(issue);
    });
  }));
  const [announcedIssueKeys, setAnnouncedIssueKeys] = createSignal<Set<string>>(loadAnnouncedIssueKeys());
  const [deletingIssue, setDeletingIssue] = createSignal<string | null>(null);
  const [batchDeleting, setBatchDeleting] = createSignal(false);
  const [selectionMode, setSelectionMode] = createSignal(false);
  const [selectedIssueKeys, setSelectedIssueKeys] = createSignal<Set<string>>(new Set());
  const [cancellingIssue, setCancellingIssue] = createSignal<string | null>(null);
  const [autoRefresh, setAutoRefresh] = createSignal(true);
  const [workspaceBrowserOpen, setWorkspaceBrowserOpen] = createSignal(false);
  const [issuePanelOpen, setIssuePanelOpen] = createSignal(false);
  const [activeTasksOpen, setActiveTasksOpen] = createSignal(true);
  const [issueFilterMode, setIssueFilterMode] = createSignal<IssueFilterMode>('workspace');
  const [lastUpdatedAt, setLastUpdatedAt] = createSignal<string>('');
  const [visibleSessionIds, setVisibleSessionIds] = createSignal<Set<string>>(new Set());
  const [observedIssueTerminalState, setObservedIssueTerminalState] = createSignal<Map<string, boolean>>(new Map());
  const [completionNotices, setCompletionNotices] = createSignal<CompletionNotice[]>([]);
  const pageVisible = createPageVisibleSignal();
  const completionNoticeTimers = new Map<string, number>();

  const issueKey = (issue: SymphonyLocalIssue) => issue.identifier || issue.id || '';

  const clearIssueSelection = () => {
    setSelectedIssueKeys(new Set<string>());
  };

  const setIssueSelectionMode = (enabled: boolean) => {
    setSelectionMode(enabled);
    if (!enabled) clearIssueSelection();
  };

  const toggleIssueSelection = (issue: SymphonyLocalIssue) => {
    const key = issueKey(issue);
    if (!key) return;
    setSelectedIssueKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else if (next.size < BATCH_DELETE_LIMIT) {
        next.add(key);
      }
      return next;
    });
  };

  const selectVisibleIssues = () => {
    setSelectedIssueKeys(
      new Set(
        issues()
          .map(issueKey)
          .filter(Boolean)
          .slice(0, BATCH_DELETE_LIMIT),
      ),
    );
  };

  const dismissCompletionNotice = (key: string) => {
    const timer = completionNoticeTimers.get(key);
    if (timer) {
      window.clearTimeout(timer);
      completionNoticeTimers.delete(key);
    }
    setCompletionNotices((prev) => prev.filter((notice) => notice.key !== key));
  };

  const openCompletionNotice = (key: string) => {
    const issue = issues().find((item) => issueKey(item) === key);
    dismissCompletionNotice(key);
    if (issue) {
      setSelectedIssueModal(issue);
      return;
    }
    setIssuePanelOpen(true);
  };

  const pushCompletionNotice = (issue: SymphonyLocalIssue, summary: string) => {
    const key = issueKey(issue);
    if (!key) return;

    const notice: CompletionNotice = {
      key,
      title: String(issue.title || '异步任务已完成').trim(),
      summary,
      state: String(issue.state || '').trim(),
    };
    setCompletionNotices((prev) => {
      const withoutCurrent = prev.filter((item) => item.key !== key);
      return [...withoutCurrent, notice].slice(-3);
    });

    const previousTimer = completionNoticeTimers.get(key);
    if (previousTimer) window.clearTimeout(previousTimer);
    const timer = window.setTimeout(() => {
      dismissCompletionNotice(key);
    }, COMPLETION_NOTICE_TTL_MS);
    completionNoticeTimers.set(key, timer);

    for (const active of completionNoticeTimers.keys()) {
      if (active === key) continue;
      if (completionNotices().some((item) => item.key === active)) continue;
      const staleTimer = completionNoticeTimers.get(active);
      if (staleTimer) window.clearTimeout(staleTimer);
      completionNoticeTimers.delete(active);
    }
  };

  onCleanup(() => {
    for (const timer of completionNoticeTimers.values()) {
      window.clearTimeout(timer);
    }
    completionNoticeTimers.clear();
  });

  const currentWorkspaceId = () => {
    const session = sessionStore.currentSession();
    const explicit = String(session?.workdir_name || '').trim();
    if (explicit) return explicit;
    const workdir = String(session?.workdir || '').trim().replace(/\/+$/, '');
    return workdir ? workdir.split('/').pop() || '' : '';
  };

  const syncVisibleSessionIds = async (options: { includePersisted?: boolean } = {}) => {
    if (!pageVisible()) {
      return visibleSessionIds();
    }
    const includePersisted = Boolean(options.includePersisted);
    const currentSessionId = sessionStore.currentSessionId();
    if (!currentSessionId) {
      if (visibleSessionIds().size > 0) {
        setVisibleSessionIds(new Set<string>());
      }
      return new Set<string>();
    }

    const nextVisible = new Set<string>([currentSessionId]);
    for (const subagent of sessionStore.subagentStates()) {
      if (subagent.parent_session_id === currentSessionId && subagent.session_id) {
        nextVisible.add(subagent.session_id);
      }
    }

    if (includePersisted) {
      try {
        const subagents = await getSubagents(currentSessionId);
        for (const item of subagents) {
          if (item.session_id) nextVisible.add(item.session_id);
        }
      } catch (e) {
        console.error('Failed to sync visible Symphony session ids:', e);
      }
    }

    const currentVisible = visibleSessionIds();
    const sameSize = currentVisible.size === nextVisible.size;
    const sameValues = sameSize && Array.from(nextVisible).every((item) => currentVisible.has(item));
    if (!sameValues) {
      setVisibleSessionIds(nextVisible);
    }
    return nextVisible;
  };

  const isAsyncAcceptedNotice = (text: string) =>
    /^Accepted async (?:symphony|bridge) job:/i.test(String(text || '').trim());

  const formatIssueChatSummary = (issue: SymphonyLocalIssue) => {
    const state = String(issue.state || '已完成').trim();
    const formatted = formatAsyncResultSummary(
      issue.result_summary,
      issue.error_summary,
    );
    if (formatted.structured && formatted.text) {
      return formatted.text;
    }

    const fallback = formatted.text || '(无摘要)';
    const firstBlock = fallback.split(/Found \d+ MCP server\(s\)/, 1)[0].trim();
    return [
      `任务状态: ${state}`,
      firstBlock || fallback,
    ].filter(Boolean).join('\n');
  };

  const extractFinalSessionResultForIssue = (issue: SymphonyLocalIssue) => {
    const key = issueKey(issue);
    if (!key) return '';

    const marker = `[Symphony:${key}]`;
    const messages = sessionStore.messages();
    const resultNoticeItems = messages
      .map((m, i) => ({ i, role: m.role, content: String(m.content || '').trim() }))
      .filter((x) => (x.role === 'assistant' || x.role === 'system') && x.content.length > 0);

    const markerIndexes = resultNoticeItems
      .filter((x) => x.content.includes(marker))
      .map((x) => x.i);

    if (markerIndexes.length === 0) return '';

    const isCandidate = (content: string) => {
      if (!content) return false;
      if (content.includes(marker)) return false;
      if (isAsyncAcceptedNotice(content)) return false;
      return true;
    };

    // Prefer an assistant message after the marker because it is usually the skill-final output.
    for (let j = markerIndexes.length - 1; j >= 0; j -= 1) {
      const markerPos = markerIndexes[j];
      for (let i = markerPos + 1; i < messages.length; i += 1) {
        const m = messages[i];
        if (!m || m.role !== 'assistant') continue;
        const content = String(m.content || '').trim();
        if (isCandidate(content)) return content;
      }
    }

    // Fallback: use the nearest assistant message before marker.
    const latestMarkerPos = markerIndexes[markerIndexes.length - 1];
    for (let i = latestMarkerPos - 1; i >= 0; i -= 1) {
      const m = messages[i];
      if (!m || m.role !== 'assistant') continue;
      const content = String(m.content || '').trim();
      if (isCandidate(content)) return content;
    }

    return '';
  };

  const issueStateClass = (state?: string) => {
    const normalized = String(state || '').toLowerCase();
    if (normalized === 'done' || normalized === 'closed' || normalized === 'completed') return 'bg-[#238636] text-white';
    if (normalized === 'in progress' || normalized === 'running' || normalized === 'processing') return 'bg-[#1f6feb] text-white';
    if (normalized === 'todo' || normalized === 'queued' || normalized === 'pending' || normalized === 'review_pending') return 'bg-[#f1a02b]/18 text-[#ffe1b3]';
    if (normalized === 'cancelled' || normalized === 'canceled' || normalized === 'failed') return 'bg-[#da3633] text-white';
    return 'bg-[#30363d] text-[#c9d1d9]';
  };
  const issueStateLabel = (state?: string, issue?: SymphonyLocalIssue) => {
    if (issue?.workspace?.recovery?.state === 'submission_unknown') return '提交状态待核对';
    if (issue?.workspace?.transfer_state === 'retry_wait') return '计算完成 · 保存待重试';
    if (issue?.workspace?.transfer_state === 'downloading') return '计算完成 · 正在保存';
    if (issue?.workspace?.cancellation?.state === 'pending') return '取消待确认 · 自动重试';
    if (issue?.workspace?.recovery?.state === 'retry_wait' && !['done','completed','closed','failed','cancelled','canceled'].includes(String(state).toLowerCase())) return '连接中断 · 正在核对';
    const normalized = String(state || '').trim().toLowerCase();
    if (isDispatchingSubmission(issue)) return '派发中 · 尚未取得远端受理回执';
    if (hasAcceptedSubmission(issue) && ['todo', 'queued', 'pending', 'open'].includes(normalized)) return '进行中 · 远端排队';
    const labels: Record<string, string> = {
      todo: '待处理',
      done: '已完成',
      completed: '已完成',
      closed: '已关闭',
      'in progress': '进行中',
      running: '进行中',
      processing: '处理中',
      cancelled: '已取消',
      canceled: '已取消',
      failed: '失败',
      queued: '排队中',
      pending: '待处理',
      review_pending: '待审核',
      open: '打开',
    };
    return labels[normalized] || state || '?';
  };

  const issueKanbanStatus = (issue: SymphonyLocalIssue): IssueKanbanStatus => {
    const state = String(issue.state || '').trim().toLowerCase();
    const trackerStatus = String(issue.tracker_status || '').trim().toLowerCase();
    if (
      state === 'done'
      || state === 'completed'
      || state === 'closed'
      || trackerStatus === 'completed'
    ) {
      return 'done';
    }
    if (
      state === 'failed'
      || state === 'error'
      || state === 'cancelled'
      || state === 'canceled'
      || state === 'duplicate'
      || trackerStatus === 'failed'
      || trackerStatus === 'cancelled'
      || trackerStatus === 'duplicate'
    ) {
      return 'failed';
    }
    if (isDispatchingSubmission(issue)) return 'todo';
    if (
      hasAcceptedSubmission(issue)
      || state === 'in progress'
      || state === 'running'
      || state === 'processing'
      || trackerStatus === 'claimed'
      || trackerStatus === 'running'
      || trackerStatus === 'retrying'
    ) {
      return 'in_progress';
    }
    return 'todo';
  };

  const issuesByKanbanStatus = createMemo(() => {
    const grouped = new Map<IssueKanbanStatus, SymphonyLocalIssue[]>();
    for (const column of ISSUE_KANBAN_COLUMNS) grouped.set(column.status, []);
    for (const issue of issues()) {
      grouped.get(issueKanbanStatus(issue))?.push(issue);
    }
    return grouped;
  });

  const extractIssueSessionIds = (issue: SymphonyLocalIssue) => {
    const workspace = issue.workspace && typeof issue.workspace === 'object'
      ? issue.workspace
      : null;
    const sessionId = String(workspace?.session_id || '').trim() || extractSessionIdFromDescription(issue.description);
    const parentSessionId = String(workspace?.parent_session_id || '').trim();
    return { sessionId, parentSessionId };
  };

  const formatTimestamp = (value?: string) => {
    const text = String(value || '').trim();
    if (!text) return '-';
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return text;
    return parsed.toLocaleString();
  };

  const issueWorkspaceLabel = (issue: SymphonyLocalIssue) => {
    const workspace = issue.workspace && typeof issue.workspace === 'object'
      ? issue.workspace
      : {};
    const explicit = String(workspace.workspace_id || '').trim();
    if (explicit) return explicit;
    const workdir = String(workspace.workdir || '').trim().replace(/\/+$/, '');
    return workdir ? workdir.split('/').pop() || '-' : '-';
  };

  const buildIssueDetailRows = (issue: SymphonyLocalIssue) => {
    const workspace = issue.workspace && typeof issue.workspace === 'object'
      ? issue.workspace
      : {};
    const { sessionId, parentSessionId } = extractIssueSessionIds(issue);
    const realJobId = String(
      workspace.job_id ||
      workspace.job_ref?.job_id ||
      workspace.job_ref?.id ||
      ''
    ).trim();
    const processId = String(workspace.process_id || workspace.job_ref?.process_id || '').trim();
    return [
      { label: '任务状态', value: issueStateLabel(issue.state, issue) },
      { label: '跟踪状态', value: String(issue.tracker_status || '').trim() },
      { label: '执行通道', value: String(issue.execution_channel || '').trim() },
      { label: '更新时间', value: formatTimestamp(issue.updated_at) },
      { label: '创建时间', value: formatTimestamp(issue.created_at) },
      { label: '会话 ID', value: sessionId },
      { label: '父会话 ID', value: parentSessionId },
      { label: '工作目录', value: String(workspace.workdir || '').trim() },
      { label: '任务工具', value: (
        workspace.tool && workspace.task_type
          ? `${workspace.tool}.${workspace.task_type}`
          : String(workspace.tool || workspace.task_type || '').trim()
      ) },
      { label: 'Job ID', value: realJobId },
      { label: 'Process ID', value: processId },
      { label: 'Run ID', value: String(workspace.run_id || '').trim() },
      { label: 'Issue 标识', value: String(workspace.issue_identifier || issue.identifier || '').trim() },
      { label: '认领方', value: String(issue.claim_owner || '').trim() },
      { label: '认领过期', value: formatTimestamp(issue.claim_expires_at) },
    ].filter((item) => String(item.value || '').trim() && String(item.value || '').trim() !== '-');
  };

  const issueMatchesVisibleSessions = (issue: SymphonyLocalIssue, sessionIds: Set<string>) => {
    if (sessionIds.size === 0) return false;
    const { sessionId, parentSessionId } = extractIssueSessionIds(issue);
    return (
      (sessionId && sessionIds.has(sessionId))
      || (parentSessionId && sessionIds.has(parentSessionId))
    );
  };

  const issueSortRank = (issue: SymphonyLocalIssue, sessionIds: Set<string>) => {
    const normalized = String(issue.state || '').trim().toLowerCase();
    const related = issueMatchesVisibleSessions(issue, sessionIds) ? 0 : 1;
    const statusRank = (
      normalized === 'todo'
      || normalized === 'queued'
      || normalized === 'pending'
      || normalized === 'review_pending'
      || normalized === 'open'
    )
      ? 0
      : (
        normalized === 'in progress'
        || normalized === 'running'
        || normalized === 'processing'
      )
        ? 1
        : (
          normalized === 'done'
          || normalized === 'completed'
          || normalized === 'closed'
        )
          ? 3
          : 2;
    const updatedAt = Date.parse(String(issue.updated_at || issue.created_at || '')) || 0;
    return { related, statusRank, updatedAt };
  };

  const sortIssuesForQueue = (items: SymphonyLocalIssue[], sessionIds: Set<string>) =>
    [...items].sort((left, right) => {
      const leftRank = issueSortRank(left, sessionIds);
      const rightRank = issueSortRank(right, sessionIds);
      if (leftRank.statusRank !== rightRank.statusRank) {
        return leftRank.statusRank - rightRank.statusRank;
      }
      if (leftRank.related !== rightRank.related) {
        return leftRank.related - rightRank.related;
      }
      if (leftRank.updatedAt !== rightRank.updatedAt) {
        return rightRank.updatedAt - leftRank.updatedAt;
      }
      return String(right.identifier || right.id || '').localeCompare(String(left.identifier || left.id || ''));
    });

  const openIssuePanel = () => {
    setIssuePanelOpen(true);
  };

  const closeIssuePanel = () => {
    setIssuePanelOpen(false);
    setExpandedIssue(null);
    setIssueSelectionMode(false);
  };

  const isTerminalIssue = (state?: string) => {
    const normalized = String(state || '').toLowerCase();
    return normalized === 'done' || normalized === 'completed' || normalized === 'closed' || normalized === 'failed' || normalized === 'cancelled' || normalized === 'canceled';
  };

  const activeIssues = createMemo(() =>
    issues().filter((issue) => {
      const status = issueKanbanStatus(issue);
      return status === 'todo' || status === 'in_progress';
    })
  );

  const issueProgress = (issue: SymphonyLocalIssue) => {
    const progress = issue.workspace?.tool_progress;
    if (!progress || typeof progress !== 'object') return null;
    const current = Number(progress.current);
    const total = Number(progress.total);
    const hasCurrent = Number.isFinite(current) && current >= 0;
    const hasTotal = Number.isFinite(total) && total > 0;
    const percent = hasCurrent && hasTotal
      ? Math.max(0, Math.min(100, Math.round((current / total) * 100)))
      : undefined;
    const unit = String(progress.unit || '').trim();
    const message = String(progress.message || '').trim();
    const value = hasCurrent
      ? hasTotal
        ? `${current}/${total}${unit ? ` ${unit}` : ''}`
        : `${current}${unit ? ` ${unit}` : ''}`
      : '';
    return {
      message,
      value,
      percent,
    };
  };

  const issueToolLabel = (issue: SymphonyLocalIssue) => {
    const workspace = issue.workspace && typeof issue.workspace === 'object'
      ? issue.workspace
      : {};
    const tool = String(workspace.tool || '').trim();
    const taskType = String(workspace.task_type || '').trim();
    return [tool, taskType].filter(Boolean).join('.') || String(issue.title || '异步 MCP 任务').trim();
  };

  const isDeletableIssue = (state?: string) => {
    const normalized = String(state || '').toLowerCase();
    return normalized === 'done' || normalized === 'completed' || normalized === 'closed' || normalized === 'failed' || normalized === 'cancelled' || normalized === 'canceled' || normalized === 'duplicate';
  };

  const isCancellableIssue = (state?: string) => {
    const normalized = String(state || '').toLowerCase();
    return normalized !== 'done' && normalized !== 'completed' && normalized !== 'closed' && normalized !== 'failed' && normalized !== 'cancelled' && normalized !== 'canceled' && normalized !== 'duplicate';
  };

  const extractSessionIdFromDescription = (description?: string) => {
    const text = String(description || '');
    const match = text.match(/session_id:\s*([^\n\r]+)/i);
    return match?.[1]?.trim() || '';
  };

  const syncTerminalIssueNotifications = (nextIssues: SymphonyLocalIssue[]) => {
    const currentSessionId = sessionStore.currentSessionId();
    if (!currentSessionId) return;
    const currentVisibleSessionIds = visibleSessionIds();

    const announced = announcedIssueKeys();
    const nextAnnounced = new Set(announced);
    const previousTerminalState = observedIssueTerminalState();
    const nextTerminalState = new Map(previousTerminalState);

    for (const issue of nextIssues) {
      const key = issueKey(issue);
      if (!key) continue;

      const terminal = isTerminalIssue(issue.state);
      const wasObserved = previousTerminalState.has(key);
      const wasTerminal = previousTerminalState.get(key) === true;
      nextTerminalState.set(key, terminal);

      if (!terminal) continue;

      if (!issueMatchesVisibleSessions(issue, currentVisibleSessionIds)) continue;
      if (nextAnnounced.has(key)) continue;

      if (!wasObserved || wasTerminal) {
        nextAnnounced.add(key);
        continue;
      }

      const summary = formatIssueChatSummary(issue);
      pushCompletionNotice(issue, summary);
      nextAnnounced.add(key);
    }

    if (nextAnnounced.size !== announced.size) {
      saveAnnouncedIssueKeys(nextAnnounced);
      setAnnouncedIssueKeys(nextAnnounced);
    }

    setObservedIssueTerminalState(nextTerminalState);
  };

  const loadIssues = async (options: { silent?: boolean; visibleIds?: Set<string> } = {}) => {
    if (!pageVisible()) {
      return;
    }
    const silent = Boolean(options.silent);
    if (!silent) setLoading(true);
    try {
      const currentSessionId = sessionStore.currentSessionId();
      if (!currentSessionId && visibleSessionIds().size > 0) {
        setVisibleSessionIds(new Set<string>());
      }

      const relatedSessionIds = options.visibleIds && options.visibleIds.size > 0
        ? options.visibleIds
        : (visibleSessionIds().size > 0
          ? visibleSessionIds()
          : (currentSessionId ? new Set<string>([currentSessionId]) : new Set<string>()));

      const filterMode = issueFilterMode();
      const data = await getSymphonyLocalIssues(
        500,
        currentSessionId || '',
        {
          sessionIds: filterMode === 'session' ? relatedSessionIds : undefined,
          workspaceId: filterMode === 'workspace' ? currentWorkspaceId() : undefined,
        },
      );
      const allIssues = Array.isArray(data.issues) ? data.issues : [];
      const nextIssues = sortIssuesForQueue(allIssues, relatedSessionIds);
      const nextSnapshot = issueListSignature(nextIssues);
      const issuesChanged = nextSnapshot !== issueSnapshot();
      if (issuesChanged) {
        setIssues(nextIssues);
        setIssueSnapshot(nextSnapshot);
      }
      const availableKeys = new Set(nextIssues.map(issueKey).filter(Boolean));
      setSelectedIssueKeys((current) => {
        const next = new Set(Array.from(current).filter((key) => availableKeys.has(key)));
        return next.size === current.size ? current : next;
      });
      if (total() !== nextIssues.length) {
        setTotal(nextIssues.length);
      }
      syncTerminalIssueNotifications(nextIssues);
      if (!silent || issuesChanged) {
        setLastUpdatedAt(new Date().toLocaleTimeString());
      }
    } catch (e) {
      console.error('Failed to load symphony issues:', e);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  const toggleIssue = (issue: SymphonyLocalIssue) => {
    const key = issueKey(issue);
    if (!key) return;
    setExpandedIssue((prev) => (prev === key ? null : key));
  };

  const deleteIssue = async (issue: SymphonyLocalIssue) => {
    const key = issueKey(issue);
    const localOnly = isUnresolvedSubmission(issue);
    if (!key || (!isDeletableIssue(issue.state) && !localOnly)) return;
    if (localOnly && !await confirmAction('此任务的远程提交状态无法确认。删除只移除本地记录，不代表远程计算已停止；删除后本应用不再自动核对该任务。已有产物不会被删除。如远端实际已启动，仍需在远程服务中核对并停止。确定删除本地记录吗？', { confirmLabel: '仅删除本地记录' })) return;
    try {
      setDeletingIssue(key);
      await deleteSymphonyLocalIssue({
        id: issue.id,
        identifier: issue.identifier,
        ...(localOnly ? { forget_unresolved_submission: true } : {}),
      });
      setIssues((prev) => prev.filter((x) => issueKey(x) !== key));
      setTotal((prev) => Math.max(0, prev - 1));
      if (expandedIssue() === key) setExpandedIssue(null);
      if (issueKey(selectedIssueModal() || ({} as SymphonyLocalIssue)) === key) {
        setSelectedIssueModal(null);
      }
    } catch (e) {
      console.error('Failed to delete symphony issue:', e);
      await showFeedback(e instanceof Error ? e.message : String(e));
    } finally {
      setDeletingIssue(null);
    }
  };

  const batchDeleteSelectedIssues = async () => {
    const selectedKeys = selectedIssueKeys();
    const selectedIssues = issues().filter((issue) => selectedKeys.has(issueKey(issue)));
    if (selectedIssues.length === 0 || batchDeleting()) return;

    const unresolvedCount = selectedIssues.filter(isUnresolvedSubmission).length;
    const activeCount = selectedIssues.filter((issue) => isCancellableIssue(issue.state) && !isUnresolvedSubmission(issue)).length;
    const terminalCount = selectedIssues.length - activeCount - unresolvedCount;
    const detail = [
      `确认删除选中的 ${selectedIssues.length} 个异步任务吗？`,
      activeCount > 0 ? `其中 ${activeCount} 个运行中任务将先停止再删除。` : '',
      terminalCount > 0 ? `${terminalCount} 个已结束任务将直接删除。` : '',
      unresolvedCount > 0 ? `${unresolvedCount} 个提交状态待核对任务仅删除本地记录，不会停止可能已启动的远程计算；删除后不再自动核对，需自行在远程服务确认。` : '',
      '此操作只删除任务记录，不删除 Protein Run 结果目录。',
    ].filter(Boolean).join('\n');
    if (!await confirmAction(detail)) return;

    try {
      setBatchDeleting(true);
      const response = await batchDeleteSymphonyLocalIssues(
        selectedIssues.map((issue) => ({ id: issue.id, identifier: issue.identifier, ...(isUnresolvedSubmission(issue) ? { forget_unresolved_submission: true } : {}) })),
        true,
      );
      const removedKeys = new Set(
        response.results
          .filter((item) => ['deleted', 'already_deleted', 'duplicate'].includes(item.status))
          .map((item) => item.identifier || item.id)
          .filter(Boolean),
      );
      const remaining = issues().filter((issue) => !removedKeys.has(issueKey(issue)));
      setIssues(remaining);
      setTotal(remaining.length);
      for (const key of removedKeys) {
        dismissCompletionNotice(key);
        if (expandedIssue() === key) setExpandedIssue(null);
        if (issueKey(selectedIssueModal() || ({} as SymphonyLocalIssue)) === key) {
          setSelectedIssueModal(null);
        }
      }

      const failedKeys = new Set(
        response.results
          .filter((item) => item.status === 'failed')
          .map((item) => item.identifier || item.id)
          .filter(Boolean),
      );
      setSelectedIssueKeys(failedKeys);
      if (failedKeys.size === 0) setIssueSelectionMode(false);

      if (response.failed_count > 0) {
        const failures = response.results
          .filter((item) => item.status === 'failed')
          .slice(0, 5)
          .map((item) => `${item.identifier || item.id}: ${item.error || '删除失败'}`)
          .join('\n');
        await showFeedback(`已删除 ${response.deleted_count} 个任务，${response.failed_count} 个失败。\n${failures}`);
      }
    } catch (e) {
      console.error('Failed to batch delete symphony issues:', e);
      await showFeedback(e instanceof Error ? e.message : String(e));
    } finally {
      setBatchDeleting(false);
    }
  };

  const cancelIssue = async (issue: SymphonyLocalIssue) => {
    const key = issueKey(issue);
    if (!key || !isCancellableIssue(issue.state)) return;
    try {
      setCancellingIssue(key);
      const response = await updateSymphonyLocalIssue({
        id: issue.id,
        identifier: issue.identifier,
        state: 'Cancelled',
      });
      const nextIssue = response.issue
        ? { ...issue, ...response.issue }
        : {
            ...issue,
            state: 'Cancelled',
            updated_at: new Date().toISOString(),
          };
      setIssues((prev) =>
        prev.map((x) => {
          if (issueKey(x) !== key) return x;
          return nextIssue;
        })
      );
      if (selectedIssueModal() && issueKey(selectedIssueModal() as SymphonyLocalIssue) === key) {
        setSelectedIssueModal(nextIssue);
      }
    } catch (e) {
      console.error('Failed to cancel symphony issue:', e);
      void loadIssues({ silent: true });
      await showFeedback(e instanceof Error ? e.message : String(e));
    } finally {
      setCancellingIssue(null);
    }
  };

  const shouldPollIssues = () =>
    props.active !== false
    &&
    autoRefresh()
    && pageVisible()
    && !workspaceBrowserOpen();

  createEffect(() => {
    if (!shouldPollIssues()) return;
    const intervalMs = activeIssues().length > 0
      ? ACTIVE_ISSUES_POLL_INTERVAL_MS
      : ISSUES_POLL_INTERVAL_MS;
    const timer = window.setInterval(() => {
      void loadIssues({ silent: true });
    }, intervalMs);
    onCleanup(() => window.clearInterval(timer));
  });

  createEffect(on(() => sessionStore.currentSessionId(), async () => {
    // Keep Symphony panel in sync with current chat session.
    const nextVisible = await syncVisibleSessionIds({ includePersisted: true });
    void loadIssues({ silent: true, visibleIds: nextVisible });
  }));

  createEffect(on(() => sessionStore.subagentsVersion(), async () => {
    const nextVisible = await syncVisibleSessionIds({ includePersisted: true });
    void loadIssues({ silent: true, visibleIds: nextVisible });
  }, { defer: true }));

  createEffect(on(issueFilterMode, async () => {
    const nextVisible = await syncVisibleSessionIds({ includePersisted: true });
    void loadIssues({ visibleIds: nextVisible });
  }, { defer: true }));

  createEffect(() => {
    const selected = selectedIssueModal();
    if (!selected) return;
    const key = issueKey(selected);
    if (!key) return;
    const latest = issues().find((issue) => issueKey(issue) === key);
    if (latest && latest !== selected) {
      setSelectedIssueModal(latest);
    }
  });

  return (
    <div class="h-full min-w-0 flex flex-col bg-[#171c20]/92">
      <div class="border-b border-[#2a3136] px-4 py-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#ffc176]">{props.jobsOnly ? '异步任务' : '运行详情'}</div>
            <div class="mt-1 text-sm font-semibold text-[#f5f7fa]">{props.jobsOnly ? '任务队列、日志与产物' : '流程与异步任务'}</div>
          </div>
          <div class="flex items-center gap-2">
            <button
              class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#9faeb7] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
              onClick={openIssuePanel}
            >
              {total()}
            </button>
          <button
            class={`rounded-full border px-2.5 py-1.5 text-[11px] font-semibold transition-colors ${autoRefresh() ? 'border-[#38b46a]/28 bg-[#38b46a]/12 text-[#9ee6bb]' : 'border-[#3e484f] bg-[#171c20] text-[#8b949e] hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]'}`}
            onClick={() => setAutoRefresh((prev) => !prev)}
            title="切换自动刷新"
          >
            {autoRefresh() ? '自动刷新 开' : '自动刷新 关'}
          </button>
          <button
            class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
            onClick={async () => {
              const nextVisible = await syncVisibleSessionIds({ includePersisted: true });
              void loadIssues({ visibleIds: nextVisible });
            }}
            disabled={loading()}
          >
            {loading() ? '刷新中...' : '刷新'}
          </button>
          <button
            class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
            onClick={openIssuePanel}
          >
            打开列表
          </button>
        </div>
      </div>
      </div>

      <Show when={activeIssues().length > 0}>
        <div class="border-b border-[#2a3136] px-4 py-3">
          <section class="overflow-hidden rounded-2xl border border-[#38bdf8]/24 bg-[#10171c]">
            <button
              type="button"
              class="flex w-full items-center justify-between gap-3 px-3.5 py-3 text-left transition-colors hover:bg-[#151d22]"
              onClick={() => setActiveTasksOpen((prev) => !prev)}
              aria-expanded={activeTasksOpen()}
            >
              <div class="flex min-w-0 items-center gap-2.5">
                <span class="relative flex h-2.5 w-2.5 shrink-0">
                  <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-[#38bdf8] opacity-35" />
                  <span class="relative inline-flex h-2.5 w-2.5 rounded-full bg-[#38bdf8]" />
                </span>
                <div class="min-w-0">
                  <div class="text-xs font-semibold text-[#dce8ef]">运行中的异步任务</div>
                  <div class="mt-0.5 text-[10px] text-[#6f7d87]">后台运行，不占用主 Agent</div>
                </div>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <span class="rounded-full border border-[#38bdf8]/24 bg-[#38bdf8]/10 px-2.5 py-0.5 text-[11px] font-semibold text-[#8ed5ff]">
                  {activeIssues().length}
                </span>
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  class={`h-4 w-4 text-[#7f8b93] transition-transform ${activeTasksOpen() ? 'rotate-90' : ''}`}
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                </svg>
              </div>
            </button>

            <Show when={activeTasksOpen()}>
              <div class="space-y-2 border-t border-[#2a3136] p-2.5">
                <For each={activeIssues()}>
                  {(issue) => {
                    const progress = () => issueProgress(issue);
                    return (
                      <button
                        type="button"
                        class="block w-full rounded-xl border border-[#2a3136] bg-[#151c21] px-3 py-2.5 text-left transition-colors hover:border-[#38bdf8]/30 hover:bg-[#182126]"
                        onClick={() => setSelectedIssueModal(issue)}
                      >
                        <div class="flex items-start justify-between gap-3">
                          <div class="min-w-0">
                            <div class="truncate text-xs font-semibold text-[#e6edf3]" title={issueToolLabel(issue)}>
                              {issueToolLabel(issue)}
                            </div>
                            <div class="mt-1 truncate font-mono text-[10px] text-[#6f9fba]">
                              {issue.identifier || issue.id}
                            </div>
                          </div>
                          <span class={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${issueStateClass(issue.state)}`}>
                            {issueStateLabel(issue.state, issue)}
                          </span>
                        </div>

                        <Show when={progress()}>
                          {(item) => (
                            <div class="mt-2.5">
                              <div class="flex items-center justify-between gap-3 text-[10px] text-[#9faeb7]">
                                <span class="min-w-0 truncate" title={item().message}>{item().message || '任务正在处理'}</span>
                                <Show when={item().value}>
                                  <span class="shrink-0 font-mono text-[#8ed5ff]">{item().value}</span>
                                </Show>
                              </div>
                              <Show when={item().percent !== undefined}>
                                <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-[#26323a]">
                                  <div
                                    class="h-full rounded-full bg-gradient-to-r from-[#1f6feb] to-[#38bdf8] transition-[width] duration-300"
                                    style={{ width: `${item().percent || 0}%` }}
                                  />
                                </div>
                              </Show>
                            </div>
                          )}
                        </Show>

                        <Show when={!progress()}>
                          <div class="mt-2 text-[10px] text-[#7f8b93]">
                            {isUnresolvedSubmission(issue) ? '提交结果尚未确认，不会自动重新提交' : issueKanbanStatus(issue) === 'todo' ? '等待执行器领取任务' : '任务正在后台执行'}
                          </div>
                        </Show>
                      </button>
                    );
                  }}
                </For>
              </div>
            </Show>
          </section>
        </div>
      </Show>

      <Show when={!props.jobsOnly}><div class="px-4 pt-4">
        <HumanReviewPanel active={props.active !== false && pageVisible() && !workspaceBrowserOpen()} />
      </div>

      <div class="tcode-scrollbar flex-1 overflow-y-auto p-4 space-y-3">
        <WorkflowWorkspacePanel active={props.active !== false && pageVisible() && !workspaceBrowserOpen()} />
      </div>
      </Show>
      <Show when={props.jobsOnly}><div class="tcode-scrollbar min-h-0 flex-1 overflow-auto p-4 space-y-3">
        <h2 class="text-sm font-semibold">全部任务 · {total()}</h2>
        <Show when={!issues().length}><p class="text-sm text-[#8b949e]">当前筛选范围暂无任务。可打开列表切换会话或工作区范围。</p></Show>
        <For each={issues()}>{issue=><button class="flex w-full items-center justify-between gap-3 rounded border border-[#303539] p-3 text-left" onClick={()=>setSelectedIssueModal(issue)}><span>{issue.title || issue.identifier || issue.id}</span><span class={issueStateClass(issue.state)}>{issueStateLabel(issue.state, issue)}</span></button>}</For>
      </div></Show>

      <Show when={completionNotices().length > 0}>
        <div class="fixed bottom-4 right-4 z-[94] flex w-[min(420px,calc(100vw-2rem))] flex-col gap-2">
          <For each={completionNotices()}>
            {(notice) => (
              <div class="overflow-hidden rounded-2xl border border-[#38b46a]/30 bg-[#10161a]/96 shadow-[0_18px_48px_rgba(0,0,0,0.42)] backdrop-blur">
                <div class="flex items-start gap-3 px-4 py-3">
                  <div class="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl border border-[#38b46a]/24 bg-[#38b46a]/12 text-[#9ee6bb]">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M20 6 9 17l-5-5" />
                    </svg>
                  </div>
                  <div class="min-w-0 flex-1">
                    <div class="flex flex-wrap items-center gap-2">
                      <div class="text-[11px] font-semibold uppercase tracking-[0.16em] text-[#9ee6bb]">异步任务完成</div>
                      <span class={`rounded-full px-2 py-0.5 text-[10px] ${issueStateClass(notice.state)}`}>
                        {issueStateLabel(notice.state)}
                      </span>
                    </div>
                    <div class="mt-1 truncate text-sm font-semibold text-[#f5f7fa]" title={notice.title}>
                      {notice.title}
                    </div>
                    <pre class="mt-2 max-h-28 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-5 text-[#bdc8d1]">{notice.summary}</pre>
                    <div class="mt-2 text-[11px] leading-5 text-[#7f8b93]">
                      关闭提醒不会删除任务，之后仍可在任务看板或“显示运行详情”中查看。
                    </div>
                    <div class="mt-3 flex items-center gap-2">
                      <button
                        type="button"
                        class="rounded-full border border-[#7bd0ff]/30 bg-[#38bdf8]/12 px-3 py-1.5 text-[11px] font-semibold text-[#8ed5ff] transition-colors hover:border-[#7bd0ff]/45 hover:text-[#bdeaff]"
                        onClick={() => openCompletionNotice(notice.key)}
                      >
                        查看任务
                      </button>
                      <button
                        type="button"
                        class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#9faeb7] transition-colors hover:border-[#7bd0ff]/30 hover:text-[#bdc8d1]"
                        onClick={() => dismissCompletionNotice(notice.key)}
                      >
                        稍后查看
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </For>
        </div>
      </Show>

      <Show when={issuePanelOpen()}>
        <Portal>
        <div class="fixed inset-0 z-[93] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" onClick={closeIssuePanel}>
          <div
            role="dialog" aria-modal="true" aria-label="异步任务看板"
            class="flex h-[min(88dvh,900px)] max-h-[calc(100dvh-2rem)] min-h-0 min-w-0 w-full max-w-[1500px] flex-col overflow-hidden rounded-[28px] border border-[#2a3136] bg-[#11171b] shadow-[0_30px_90px_rgba(0,0,0,0.5)]"
            onClick={(e) => e.stopPropagation()}
          >
            <div class="flex flex-wrap items-center justify-between gap-3 border-b border-[#2a3136] px-5 py-4">
              <div>
                <div class="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#ffc176]">Symphony Kanban</div>
                <div class="mt-1 text-sm font-semibold text-[#f5f7fa]">异步任务看板</div>
              </div>
              <div class="flex items-center gap-2">
                <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#9faeb7]">{issues().length} 项</span>
                <button
                  class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-50"
                  onClick={async () => {
                    const nextVisible = await syncVisibleSessionIds({ includePersisted: true });
                    void loadIssues({ visibleIds: nextVisible });
                  }}
                  disabled={loading()}
                >
                  {loading() ? '刷新中...' : '刷新'}
                </button>
                <button class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]" onClick={closeIssuePanel}>关闭</button>
              </div>
            </div>

            <div class="flex flex-wrap items-center justify-between gap-3 border-b border-[#2a3136] bg-[#10161a] px-5 py-3">
              <div class="flex flex-wrap items-center gap-2">
                <For
                  each={[
                    { value: 'session' as const, label: '当前 Session' },
                    { value: 'workspace' as const, label: '当前 Workspace' },
                    { value: 'all' as const, label: '全部可见' },
                  ]}
                >
                  {(filter) => (
                    <button
                      type="button"
                      class={`rounded-full border px-3 py-1.5 text-[11px] font-semibold transition-colors ${
                        issueFilterMode() === filter.value
                          ? 'border-[#7bd0ff]/35 bg-[#38bdf8]/12 text-[#8ed5ff]'
                          : 'border-[#3e484f] bg-[#171c20] text-[#8b949e] hover:border-[#7bd0ff]/25 hover:text-[#bdc8d1]'
                      }`}
                      onClick={() => setIssueFilterMode(filter.value)}
                      disabled={filter.value === 'workspace' && !currentWorkspaceId()}
                    >
                      {filter.label}
                    </button>
                  )}
                </For>
              </div>
              <div class="flex flex-wrap items-center justify-end gap-2">
                <Show
                  when={selectionMode()}
                  fallback={(
                    <button
                      type="button"
                      class="rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/10 px-3 py-1.5 text-[11px] font-semibold text-[#ffd0cb] transition-colors hover:bg-[#ff9c96]/16"
                      onClick={() => setIssueSelectionMode(true)}
                      disabled={issues().length === 0}
                    >
                      多选删除
                    </button>
                  )}
                >
                  <span class="text-[11px] text-[#9faeb7]">已选 {selectedIssueKeys().size} 项</span>
                  <button
                    type="button"
                    class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#bdc8d1] disabled:opacity-50"
                    onClick={selectVisibleIssues}
                    disabled={issues().length === 0 || batchDeleting()}
                    title={`单次最多选择 ${BATCH_DELETE_LIMIT} 项`}
                  >
                    {issues().length > BATCH_DELETE_LIMIT ? `选择前 ${BATCH_DELETE_LIMIT} 项` : '全选'}
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#8b949e] disabled:opacity-50"
                    onClick={clearIssueSelection}
                    disabled={selectedIssueKeys().size === 0 || batchDeleting()}
                  >
                    清空
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-[#ff9c96]/32 bg-[#da3633]/18 px-3 py-1.5 text-[11px] font-semibold text-[#ffd0cb] transition-colors hover:bg-[#da3633]/28 disabled:cursor-not-allowed disabled:opacity-50"
                    onClick={() => void batchDeleteSelectedIssues()}
                    disabled={selectedIssueKeys().size === 0 || batchDeleting()}
                  >
                    {batchDeleting() ? '删除中...' : `删除已选 (${selectedIssueKeys().size})`}
                  </button>
                  <button
                    type="button"
                    class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[11px] font-semibold text-[#8b949e] disabled:opacity-50"
                    onClick={() => setIssueSelectionMode(false)}
                    disabled={batchDeleting()}
                  >
                    取消多选
                  </button>
                </Show>
                <div class="text-[11px] text-[#6f7d87]">
                  <Show when={issueFilterMode() === 'session'}>Session: {sessionStore.currentSessionId() || '-'}</Show>
                  <Show when={issueFilterMode() === 'workspace'}>Workspace: {currentWorkspaceId() || '-'}</Show>
                  <Show when={issueFilterMode() === 'all'}>当前用户可见范围</Show>
                  <Show when={lastUpdatedAt()}> · 更新于 {lastUpdatedAt()}</Show>
                </div>
              </div>
            </div>

            <div class="tcode-scrollbar min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-4">
              <div class="grid h-full min-w-[900px] grid-cols-4 gap-3">
                <For each={ISSUE_KANBAN_COLUMNS}>
                  {(column) => (
                    <section class="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-[#2a3136] bg-[#13191d]">
                      <div class="flex items-center justify-between border-b border-[#2a3136] bg-[#10161a] px-3.5 py-3">
                        <div>
                          <div class={`flex items-center gap-2 text-sm font-semibold ${column.accent}`}>
                            <span class={`h-2.5 w-2.5 rounded-full ${column.dot}`} />
                            {column.label}
                          </div>
                          <div class="mt-1 text-[10px] text-[#6f7d87]">{column.description}</div>
                        </div>
                        <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-0.5 text-[11px] text-[#9faeb7]">
                          {issuesByKanbanStatus().get(column.status)?.length || 0}
                        </span>
                      </div>

                      <div class="tcode-scrollbar min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
                        <Show
                          when={(issuesByKanbanStatus().get(column.status)?.length || 0) > 0}
                          fallback={<div class="rounded-2xl border border-dashed border-[#303539] px-4 py-8 text-center text-xs text-[#6f7d87]">暂无任务</div>}
                        >
                          <For each={issuesByKanbanStatus().get(column.status) || []}>
                            {(issue) => (
                              <article class={`overflow-hidden rounded-2xl border bg-[#171c20] shadow-[0_12px_28px_rgba(0,0,0,0.16)] ${selectedIssueKeys().has(issueKey(issue)) ? 'border-[#ff9c96]/55 ring-1 ring-[#ff9c96]/20' : 'border-[#2a3136]'}`}>
                                <div class="flex items-start">
                                  <Show when={selectionMode()}>
                                    <label
                                      class="flex shrink-0 cursor-pointer items-center self-stretch px-3 transition-colors hover:bg-[#1a2126]"
                                      title="选择此任务"
                                    >
                                      <input
                                        type="checkbox"
                                        class="h-4 w-4 cursor-pointer accent-[#da3633]"
                                        checked={selectedIssueKeys().has(issueKey(issue))}
                                        disabled={batchDeleting() || (selectedIssueKeys().size >= BATCH_DELETE_LIMIT && !selectedIssueKeys().has(issueKey(issue)))}
                                        onChange={() => toggleIssueSelection(issue)}
                                      />
                                    </label>
                                  </Show>
                                  <button
                                    type="button"
                                    class="block min-w-0 flex-1 px-3.5 py-3 text-left transition-colors hover:bg-[#1a2126]"
                                    onClick={() => toggleIssue(issue)}
                                  >
                                  <div class="flex items-start justify-between gap-2">
                                    <span class="min-w-0 truncate font-mono text-[10px] text-[#7bd0ff]">{issue.identifier || issue.id}</span>
                                    <span class={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${issueStateClass(issue.state)}`}>
                                      {issueStateLabel(issue.state, issue)}
                                    </span>
                                  </div>
                                  <div class="mt-2 line-clamp-3 text-sm font-semibold leading-5 text-[#f1f5f8]">{issue.title || '(未命名)'}</div>
                                  <div class="mt-3 flex items-center justify-between gap-2 text-[10px] text-[#6f7d87]">
                                    <span class="truncate" title={issueWorkspaceLabel(issue)}>Workspace: {issueWorkspaceLabel(issue)}</span>
                                    <span>{formatTimestamp(issue.updated_at)}</span>
                                  </div>
                                  <div class="mt-2 flex items-center gap-1.5 text-[10px] text-[#8b949e]">
                                    <svg
                                      xmlns="http://www.w3.org/2000/svg"
                                      class={`h-3 w-3 transition-transform ${expandedIssue() === issueKey(issue) ? 'rotate-90' : ''}`}
                                      fill="none"
                                      viewBox="0 0 24 24"
                                      stroke="currentColor"
                                    >
                                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                                    </svg>
                                    {expandedIssue() === issueKey(issue) ? '收起详情' : '展开详情'}
                                  </div>
                                  </button>
                                </div>

                                <Show when={expandedIssue() === issueKey(issue)}>
                                  <div class="space-y-3 border-t border-[#2a3136] bg-[#13191d] px-3.5 py-3">
                                    <For each={buildIssueDetailRows(issue)}>
                                      {(row) => (
                                        <div class="rounded-xl border border-[#2a3136] bg-[#10161a] px-3 py-2">
                                          <div class="text-[10px] text-[#6f7d87]">{row.label}</div>
                                          <div class="mt-1 break-words text-[11px] text-[#d5dde3]">{row.value}</div>
                                        </div>
                                      )}
                                    </For>
                                    <Show when={formatAsyncResultSummary(issue.result_summary).text}>
                                      <div>
                                        <div class="mb-1.5 text-[10px] text-[#8b949e]">结果</div>
                                        <div class="tcode-scrollbar max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[#238636]/30 bg-[#0f1913] p-3 text-[11px] text-[#9ee6bb]">{formatAsyncResultSummary(issue.result_summary).text}</div>
                                      </div>
                                    </Show>
                                    <Show when={issue.error_summary}>
                                      <div>
                                        <div class="mb-1.5 text-[10px] text-[#8b949e]">错误</div>
                                        <div class="tcode-scrollbar max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[#5a1e23] bg-[#201317] p-3 text-[11px] text-[#ffb6b0]">{issue.error_summary}</div>
                                      </div>
                                    </Show>
                                    <JobWorkspacePanel
                                      workspace={issue.workspace}
                                      title="任务工作区"
                                      onBrowserOpenChange={setWorkspaceBrowserOpen}
                                      onActionComplete={() => {
                                        void loadIssues();
                                      }}
                                    />
                                    <div class="flex flex-wrap items-center gap-2 pt-1">
                                      <button
                                        class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-2 text-[11px] font-semibold text-[#bdc8d1]"
                                        onClick={() => setSelectedIssueModal(issue)}
                                      >
                                        查看完整结果
                                      </button>
                                      <Show when={isCancellableIssue(issue.state) && !isUnresolvedSubmission(issue)}>
                                        <button
                                          class="rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/12 px-3 py-2 text-[11px] font-semibold text-[#ffe1b3]"
                                          onClick={() => cancelIssue(issue)}
                                          disabled={cancellingIssue() === issueKey(issue)}
                                        >
                                          {cancellingIssue() === issueKey(issue) ? '正在确认...' : issue.workspace?.cancellation?.state === 'pending' ? '重试取消' : '停止任务'}
                                        </button>
                                      </Show>
                                      <Show when={isDeletableIssue(issue.state) || isUnresolvedSubmission(issue)}>
                                        <button
                                          class="rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/12 px-3 py-2 text-[11px] font-semibold text-[#ffd0cb]"
                                          onClick={() => deleteIssue(issue)}
                                          disabled={deletingIssue() === issueKey(issue)}
                                        >
                                          {deletingIssue() === issueKey(issue) ? '删除中...' : isUnresolvedSubmission(issue) ? '删除本地记录' : '删除任务'}
                                        </button>
                                      </Show>
                                    </div>
                                  </div>
                                </Show>
                              </article>
                            )}
                          </For>
                        </Show>
                      </div>
                    </section>
                  )}
                </For>
              </div>
            </div>
          </div>
        </div>
        </Portal>
      </Show>

      <Show when={selectedIssueModal()}>
        <Portal>
        <div class="fixed inset-0 bg-black/60 z-[95] flex items-center justify-center p-4 backdrop-blur-sm" onClick={() => setSelectedIssueModal(null)}>
          <div role="dialog" aria-modal="true" aria-label="异步任务详情" class="flex min-h-0 min-w-0 w-full max-w-3xl max-h-[calc(100dvh-2rem)] flex-col bg-[#11171b] border border-[#2a3136] rounded-[28px] overflow-hidden shadow-[0_28px_80px_rgba(0,0,0,0.46)]" onClick={(e) => e.stopPropagation()}>
            <div class="px-5 py-4 border-b border-[#2a3136] bg-[#10161a] flex items-center justify-between">
              <div class="min-w-0">
                <div class="text-xs text-[#8b949e] truncate">{selectedIssueModal()?.identifier || selectedIssueModal()?.id}</div>
                <div class="text-sm font-semibold text-[#e6edf3] truncate">{selectedIssueModal()?.title || '(未命名)'}</div>
              </div>
              <button class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-2 text-xs font-semibold text-[#bdc8d1]" onClick={() => setSelectedIssueModal(null)}>关闭</button>
            </div>
            <div class="tcode-scrollbar min-h-0 p-5 space-y-4 overflow-auto">
              <div>
                <span class={`text-[11px] px-3 py-1 rounded-full ${issueStateClass(selectedIssueModal()?.state)}`}>
                  {issueStateLabel(selectedIssueModal()?.state, selectedIssueModal() || undefined)}
                </span>
              </div>
              <Show when={formatAsyncResultSummary(selectedIssueModal()?.result_summary).text}>
                <div>
                  <div class="text-xs text-[#8b949e] mb-1">协同队列结果（MCP）</div>
                  <pre class="text-xs text-[#9ee6bb] whitespace-pre-wrap break-words bg-[#0d1216] border border-[#2a3136] rounded-2xl p-4">{formatAsyncResultSummary(selectedIssueModal()?.result_summary).text}</pre>
                </div>
              </Show>
              <Show when={selectedIssueModal() && extractFinalSessionResultForIssue(selectedIssueModal() as SymphonyLocalIssue)}>
                <div>
                  <div class="text-xs text-[#8b949e] mb-1">最终结果（技能）</div>
                  <pre class="text-xs text-[#9fdcff] whitespace-pre-wrap break-words bg-[#0d1216] border border-[#2a3136] rounded-2xl p-4">{extractFinalSessionResultForIssue(selectedIssueModal() as SymphonyLocalIssue)}</pre>
                </div>
              </Show>
              <Show when={selectedIssueModal()?.error_summary}>
                <div>
                  <div class="text-xs text-[#8b949e] mb-1">错误</div>
                  <pre class="text-xs text-[#ffb6b0] whitespace-pre-wrap break-words bg-[#201317] border border-[#5a1e23] rounded-2xl p-4">{selectedIssueModal()?.error_summary}</pre>
                </div>
              </Show>
              <JobWorkspacePanel
                workspace={selectedIssueModal()?.workspace}
                title="任务工作区"
                onBrowserOpenChange={setWorkspaceBrowserOpen}
                onActionComplete={() => {
                  void loadIssues();
                }}
              />
              <Show when={isUnresolvedSubmission(selectedIssueModal())}>
                <p class="text-xs text-[#ffe1b3]">远程提交状态尚未确认，无法凭本地记录确认停止。可以删除本地记录，但这不会停止可能已启动的远程计算。</p>
              </Show>
              <Show when={isDeletableIssue(selectedIssueModal()?.state) || isUnresolvedSubmission(selectedIssueModal())}>
                <div class="pt-3 border-t border-[#2a3136]">
                  <button
                    class="rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/12 px-4 py-2 text-sm font-semibold text-[#ffd0cb]"
                    onClick={() => {
                      const issue = selectedIssueModal();
                      if (issue) void deleteIssue(issue);
                    }}
                    disabled={deletingIssue() === issueKey(selectedIssueModal() || ({} as SymphonyLocalIssue))}
                  >
                    {deletingIssue() === issueKey(selectedIssueModal() || ({} as SymphonyLocalIssue)) ? '删除中...' : isUnresolvedSubmission(selectedIssueModal()) ? '删除本地记录' : '删除此已完成/失败/取消任务'}
                  </button>
                </div>
              </Show>
              <Show when={isCancellableIssue(selectedIssueModal()?.state) && !isUnresolvedSubmission(selectedIssueModal())}>
                <div class="pt-3 border-t border-[#2a3136]">
                  <button
                    class="rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/12 px-4 py-2 text-sm font-semibold text-[#ffe1b3]"
                    onClick={() => {
                      const issue = selectedIssueModal();
                      if (issue) void cancelIssue(issue);
                    }}
                    disabled={cancellingIssue() === issueKey(selectedIssueModal() || ({} as SymphonyLocalIssue))}
                  >
                    {cancellingIssue() === issueKey(selectedIssueModal() || ({} as SymphonyLocalIssue)) ? '停止中...' : '停止此任务'}
                  </button>
                </div>
              </Show>
            </div>
          </div>
        </div>
        </Portal>
      </Show>
    </div>
  );
}
