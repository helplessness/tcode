import { For, Show, createEffect, createMemo, createSignal, on, onCleanup, onMount } from 'solid-js';
import { Portal } from 'solid-js/web';
import {
  approveHumanReview,
  deleteHumanReview,
  getSubagents,
  getSession,
  getHumanReviewConfig,
  getHumanReviewQueue,
  rejectHumanReview,
  type HumanReviewItem,
} from '../api/client';
import { sessionStore } from '../stores/session';
import { createPageVisibleSignal } from '../utils/pageVisibility';
import JobWorkspacePanel from './JobWorkspacePanel';
import { reviewPhase, reviewSummary } from './HumanReviewCard';
import { toolDisplayLabel } from '../utils/userFacingLabels';

const RESUME_POLL_INTERVAL_MS = 1500;
const RESUME_POLL_ATTEMPTS = 40;
const REVIEW_POLL_INTERVAL_MS = 8000;

type ReviewFlowStatus = 'todo' | 'process' | 'done' | 'fail';

const REVIEW_FLOW_COLUMNS: Array<{
  status: ReviewFlowStatus;
  label: string;
  description: string;
  accent: string;
  dot: string;
  badge: string;
  empty: string;
}> = [
  {
    status: 'todo',
    label: 'Todo',
    description: '等待人工审核',
    accent: 'text-[#ffe1b3]',
    dot: 'bg-[#f1a02b]',
    badge: 'border-[#f1a02b]/28 bg-[#f1a02b]/12 text-[#ffe1b3]',
    empty: '暂无待审核项',
  },
  {
    status: 'process',
    label: 'Process',
    description: '审核后恢复中',
    accent: 'text-[#8ed5ff]',
    dot: 'bg-[#38bdf8]',
    badge: 'border-[#38bdf8]/28 bg-[#38bdf8]/12 text-[#8ed5ff]',
    empty: '暂无恢复中项',
  },
  {
    status: 'done',
    label: 'Done',
    description: '已通过并处理',
    accent: 'text-[#9ee6bb]',
    dot: 'bg-[#38b46a]',
    badge: 'border-[#38b46a]/28 bg-[#38b46a]/12 text-[#9ee6bb]',
    empty: '暂无已通过记录',
  },
  {
    status: 'fail',
    label: 'Fail',
    description: '失败或已驳回',
    accent: 'text-[#ffb6b0]',
    dot: 'bg-[#da3633]',
    badge: 'border-[#ff9c96]/28 bg-[#ff9c96]/12 text-[#ffd0cb]',
    empty: '暂无失败或驳回记录',
  },
];

type HumanReviewPanelProps = {
  active?: boolean;
  focusRequest?: {reviewId?: string; reviewIdentifier?: string};
};

function normalizeSessionMessages(messages: any[]) {
  if (!Array.isArray(messages)) return [];
  return messages
    .filter((item) => {
      const role = String(item?.role || '').trim().toLowerCase();
      return role === 'user' || role === 'assistant' || role === 'system';
    })
    .map((item) => ({
      ...item,
      role: String(item.role || 'assistant') as 'user' | 'assistant' | 'system',
    }));
}

function resumeStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: '排队中',
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    skipped: '已跳过',
  };
  const normalized = String(status || '').trim().toLowerCase();
  return labels[normalized] || status;
}

function reviewFlowStatus(review: HumanReviewItem): ReviewFlowStatus {
  const status = String(review.status || '').trim().toLowerCase();
  const resumeStatus = String(review.resume_status || '').trim().toLowerCase();
  if (status === 'pending_review') return 'todo';
  if (status === 'rejected' || status === 'cancelled' || status === 'canceled') return 'fail';
  if (resumeStatus === 'failed') return 'fail';
  if (resumeStatus === 'queued' || resumeStatus === 'running') return 'process';
  if (status === 'approved') return 'done';
  if (status === 'deleted') return 'fail';
  return 'process';
}

function reviewFlowLabel(review: HumanReviewItem) {
  if (review.review_data?.goal_computation) return reviewPhase(review);
  const flow = reviewFlowStatus(review);
  if (flow === 'todo') return '待处理';
  if (flow === 'process') return '恢复中';
  if (flow === 'done') return '已通过';
  return String(review.status || '').toLowerCase() === 'rejected' ? '已驳回' : '失败';
}

export default function HumanReviewPanel(props: HumanReviewPanelProps = {}) {
  const [reviews, setReviews] = createSignal<HumanReviewItem[]>([]);
  const [reviewSnapshot, setReviewSnapshot] = createSignal('');
  const [loading, setLoading] = createSignal(false);
  const [queueLoaded, setQueueLoaded] = createSignal(false);
  const [queueError, setQueueError] = createSignal('');
  let queueRevision = 0;
  const [enabled, setEnabled] = createSignal(false);
  const [lastUpdatedAt, setLastUpdatedAt] = createSignal('');
  const [autoRefresh, setAutoRefresh] = createSignal(false);
  const [workspaceBrowserOpen, setWorkspaceBrowserOpen] = createSignal(false);
  const [reviewPanelOpen, setReviewPanelOpen] = createSignal(false);
  const [expandedReviewKey, setExpandedReviewKey] = createSignal<string | null>(null);
  const [selectedReview, setSelectedReview] = createSignal<HumanReviewItem | null>(null);
  const [reviewFeedback, setReviewFeedback] = createSignal('');
  const [focusError, setFocusError] = createSignal('');
  const [resumeInstruction, setResumeInstruction] = createSignal('');
  const [overrideJson, setOverrideJson] = createSignal('');
  const [rejectReason, setRejectReason] = createSignal('');
  const [submitting, setSubmitting] = createSignal(false);
  const [visibleSessionIds, setVisibleSessionIds] = createSignal<Set<string>>(new Set());
  const [configLoaded, setConfigLoaded] = createSignal(false);
  const pageVisible = createPageVisibleSignal();
  const isActive = () => (props.active ?? true) && pageVisible();

  const reviewListSignature = (items: HumanReviewItem[]) =>
    JSON.stringify(
      (Array.isArray(items) ? items : []).map((review) => ({
        id: String(review.id || ''),
        identifier: String(review.identifier || ''),
        status: String(review.status || ''),
        updated_at: String(review.updated_at || ''),
        tool_server: String(review.tool_server || ''),
        tool_name: String(review.tool_name || ''),
        reason: String(review.reason || ''),
        resume_status: String(review.resume_status || ''),
      }))
    );

  const reviewKey = (review: HumanReviewItem) => review.identifier || review.id || '';
  const isGoalReview = (review: HumanReviewItem | null) => Boolean(review?.review_data?.goal_computation);

  const rebuildVisibleSessionIds = async (options: { includePersisted?: boolean } = {}) => {
    if (!isActive()) {
      return;
    }
    const includePersisted = Boolean(options.includePersisted);
    const currentSessionId = sessionStore.currentSessionId();
    if (!currentSessionId) {
      setVisibleSessionIds(new Set<string>());
      return;
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
        for (const subagent of subagents) {
          if (subagent.session_id) {
            nextVisible.add(subagent.session_id);
          }
        }
      } catch (e) {
        console.error('Failed to load subagents for human review visibility:', e);
      }
    }

    if (currentSessionId !== sessionStore.currentSessionId() || !isActive()) return;
    const current = visibleSessionIds();
    const sameSize = current.size === nextVisible.size;
    const sameValues = sameSize && Array.from(nextVisible).every((item) => current.has(item));
    if (!sameValues) {
      setVisibleSessionIds(nextVisible);
    }
  };

  const isVisibleForCurrentScope = (review: HumanReviewItem) => {
    const currentSessionId = sessionStore.currentSessionId();
    if (!currentSessionId) return false;

    const reviewData = review.review_data || {};
    const reviewSessionId = String(reviewData.session_id || '').trim();
    const reviewParentSessionId = String(reviewData.parent_session_id || '').trim();
    const currentVisibleSessionIds = visibleSessionIds();

    if (reviewParentSessionId && reviewParentSessionId === currentSessionId) {
      return true;
    }

    if (!reviewSessionId) {
      return false;
    }

    return currentVisibleSessionIds.has(reviewSessionId);
  };

  const isForCurrentSession = (review: HumanReviewItem) => {
    return isVisibleForCurrentScope(review);
  };

  const visibleReviews = () => reviews().filter(isForCurrentSession);
  const pendingVisibleReviews = () => visibleReviews().filter((review) => reviewFlowStatus(review) === 'todo');
  const activeVisibleReviews = () => visibleReviews().filter((review) => {
    const flow = reviewFlowStatus(review);
    return flow === 'todo' || flow === 'process';
  });
  const visibleReviewsByFlow = createMemo(() => {
    const grouped = new Map<ReviewFlowStatus, HumanReviewItem[]>();
    for (const column of REVIEW_FLOW_COLUMNS) grouped.set(column.status, []);
    for (const review of visibleReviews()) {
      grouped.get(reviewFlowStatus(review))?.push(review);
    }
    return grouped;
  });

  const reviewWorkspace = (review: HumanReviewItem | null) => {
    if (!review) return null;
    const reviewData = review.review_data || {};
    if (reviewData.workspace && typeof reviewData.workspace === 'object') {
      return reviewData.workspace;
    }
    return {
      tool: review.tool_server,
      task_type: review.tool_name,
      workdir: String(reviewData.workdir || '').trim(),
      session_id: String(reviewData.session_id || '').trim(),
      parent_session_id: String(reviewData.parent_session_id || '').trim(),
      status: review.status,
      preview_ports: [],
      preview_urls: [],
      expected_artifacts: [],
    };
  };

  const reviewWorkspaceLabel = (review: HumanReviewItem) => {
    const workspace = reviewWorkspace(review) as Record<string, any> | null;
    const raw =
      String(workspace?.workspace_id || '').trim()
      || String(workspace?.workdir || '').trim()
      || String(review.review_data?.workdir || '').trim()
      || String(review.review_data?.session_id || '').trim()
      || '-';
    if (!raw || raw === '-') return '-';
    const normalized = raw.replace(/\\/g, '/').replace(/\/+$/, '');
    return normalized.split('/').pop() || normalized;
  };

  const formatReviewTimestamp = (value?: string | null) => {
    const text = String(value || '').trim();
    if (!text) return '-';
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return text;
    return parsed.toLocaleString();
  };

  const openReviewPanel = () => {
    setReviewPanelOpen(true);
  };

  const closeReviewPanel = () => {
    setReviewPanelOpen(false);
    setExpandedReviewKey(null);
  };

  const toggleExpandedReview = (review: HumanReviewItem) => {
    const key = reviewKey(review);
    if (!key) return;
    setExpandedReviewKey((prev) => (prev === key ? null : key));
  };

  const reviewSummaryRows = (review: HumanReviewItem) => {
    if (isGoalReview(review)) return [{label:'状态', value:reviewPhase(review)}, ...reviewSummary(review)];
    const data = review.review_data || {};
    return [
      { label: '工具', value: `${review.tool_server}.${review.tool_name}` },
      { label: '状态', value: String(review.status || '').trim() },
      { label: '恢复状态', value: review.resume_status ? resumeStatusLabel(review.resume_status) : '' },
      { label: '创建时间', value: formatReviewTimestamp(review.created_at) },
      { label: '更新时间', value: formatReviewTimestamp(review.updated_at) },
      { label: '会话 ID', value: String(data.session_id || '').trim() },
      { label: '父会话 ID', value: String(data.parent_session_id || '').trim() },
      { label: '工作目录', value: String(data.workdir || '').trim() },
    ].filter((item) => String(item.value || '').trim() && String(item.value || '').trim() !== '-');
  };

  const openReview = (review: HumanReviewItem) => {
    setSelectedReview(review);
    setReviewFeedback(String(review.comments || ''));
    setResumeInstruction(String(review.resume_instruction || ''));
    if (!isGoalReview(review) && review.approved_tool_args_override && Object.keys(review.approved_tool_args_override).length > 0) {
      setOverrideJson(JSON.stringify(review.approved_tool_args_override, null, 2));
    } else {
      setOverrideJson('');
    }
    setRejectReason(String(review.rejection_reason || ''));
  };

  const closeReview = () => {
    setSelectedReview(null);
    setReviewFeedback('');
    setResumeInstruction('');
    setOverrideJson('');
    setRejectReason('');
  };

  const loadReviews = async (options: { silent?: boolean; includeConfig?: boolean } = {}) => {
    if (!isActive()) {
      return reviews();
    }
    const silent = Boolean(options.silent);
    const includeConfig = Boolean(options.includeConfig);
    const sessionId = sessionStore.currentSessionId();
    const revision = ++queueRevision;
    if (!silent) setLoading(true);
    try {
      if (includeConfig || !configLoaded()) {
        const config = await getHumanReviewConfig();
        if (enabled() !== Boolean(config.enabled)) {
          setEnabled(Boolean(config.enabled));
        }
        setConfigLoaded(true);
      }

      const queue = await getHumanReviewQueue('', 200, {sessionId: sessionId || undefined});
      if (revision !== queueRevision || sessionId !== sessionStore.currentSessionId()) return reviews();
      setQueueLoaded(true); setQueueError('');
      const nextReviews = Array.isArray(queue.reviews) ? queue.reviews : [];
      const nextSnapshot = reviewListSignature(nextReviews);
      const reviewsChanged = nextSnapshot !== reviewSnapshot();
      if (reviewsChanged) {
        setReviews(nextReviews);
        setReviewSnapshot(nextSnapshot);
      }
      if (!silent || reviewsChanged) {
        setLastUpdatedAt(new Date().toLocaleTimeString());
      }
      return nextReviews;
    } catch (e) {
      console.error('Failed to load human reviews:', e);
      if (revision === queueRevision && sessionId === sessionStore.currentSessionId()) setQueueError('审核记录加载失败，请重试；不能据此判断没有待审申请。');
      return reviews();
    } finally {
      if (!silent && revision === queueRevision) setLoading(false);
    }
  };

  const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

  const syncSessionMessages = async (sessionId: string) => {
    if (!sessionId) return false;
    const session = await getSession(sessionId);
    if (!session) return false;

    const nextMessages = normalizeSessionMessages(session.messages || []);
    sessionStore.setSessions((prev) => {
      const next = [...prev];
      const index = next.findIndex((item) => item.session_id === sessionId);
      if (index >= 0) {
        next[index] = {
          ...next[index],
          messages: nextMessages,
          updated_at: Date.now(),
        };
      }
      return next;
    });

    if (sessionStore.currentSessionId() === sessionId) {
      sessionStore.setMessages(nextMessages);
    }

    return true;
  };

  const waitForResumeCompletion = async (review: HumanReviewItem) => {
    const reviewId = String(review.id || '').trim();
    const reviewIdentifier = String(review.identifier || '').trim();
    const reviewSessionId = String((review.review_data || {}).session_id || '').trim();

    for (let attempt = 0; attempt < RESUME_POLL_ATTEMPTS; attempt += 1) {
      await sleep(RESUME_POLL_INTERVAL_MS);

      let queue;
      try { queue = await getHumanReviewQueue('', 1, {reviewId, reviewIdentifier, sessionId: reviewSessionId}); }
      catch { setQueueError('审核后的状态暂时无法读取，请刷新；不要重复批准或提交。'); return; }
      const latest = (queue.reviews || []).find((item) => {
        const sameId = reviewId && String(item.id || '').trim() === reviewId;
        const sameIdentifier = reviewIdentifier && String(item.identifier || '').trim() === reviewIdentifier;
        return sameId || sameIdentifier;
      });

      if (!latest) continue;

      const resumeStatus = String(latest.resume_status || '').trim().toLowerCase();
      if (!resumeStatus || resumeStatus === 'queued' || resumeStatus === 'running') {
        continue;
      }

      if (resumeStatus === 'completed') {
        await syncSessionMessages(reviewSessionId);
        sessionStore.setStatus('审核后的自动恢复已完成，已同步最新结果');
      } else if (resumeStatus === 'failed') {
        const errorText = String((latest.resume_result || {}).error || '').trim();
        sessionStore.setStatus(errorText ? `审核后恢复失败: ${errorText}` : '审核后恢复失败');
      } else if (resumeStatus === 'skipped') {
        const reason = String((latest.resume_result || {}).reason || '').trim();
        sessionStore.setStatus(reason ? `审核后恢复未执行: ${reason}` : '审核后恢复未执行');
      } else {
        sessionStore.setStatus(`审核恢复状态: ${resumeStatusLabel(resumeStatus)}`);
      }

      await loadReviews();
      return;
    }

    if (reviewSessionId) {
      await syncSessionMessages(reviewSessionId);
    }
    sessionStore.setStatus('审核已通过，任务已在后台恢复；如结果未显示，请稍后刷新当前会话');
    await loadReviews();
  };

  const approve = async () => {
    const current = selectedReview();
    if (!current) return;

    let parsedOverride: Record<string, any> | undefined;
    const rawOverride = overrideJson().trim();
    if (rawOverride) {
      try {
        const value = JSON.parse(rawOverride);
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
          sessionStore.setStatus('参数覆盖必须是 JSON 对象');
          return;
        }
        parsedOverride = value as Record<string, any>;
      } catch {
        sessionStore.setStatus('参数覆盖 JSON 解析失败');
        return;
      }
    }

    setSubmitting(true);
    try {
      const hasRevisionGuidance = Boolean(reviewFeedback().trim() || resumeInstruction().trim());
      const executesOverrideImmediately = Boolean(parsedOverride && Object.keys(parsedOverride).length > 0);
      await approveHumanReview({
        issue_id: current.id,
        issue_identifier: current.identifier,
        comments: reviewFeedback().trim() || undefined,
        approved_tool_args_override: parsedOverride,
        resume_after_approval: true,
        resume_instruction: resumeInstruction().trim() || undefined,
        apply_feedback_before_execution: hasRevisionGuidance && !executesOverrideImmediately,
      });
      sessionStore.setStatus(
        hasRevisionGuidance && !executesOverrideImmediately
          ? '审核已通过，原调用未提交；代理正在根据反馈修正'
          : '审核已通过，正在等待自动恢复结果'
      );
      closeReview();
      await loadReviews();
      void waitForResumeCompletion(current);
    } catch (e) {
      console.error('Approve review failed:', e);
      sessionStore.setStatus(e instanceof Error ? e.message : '审核通过失败');
    } finally {
      setSubmitting(false);
    }
  };

  const reject = async () => {
    const current = selectedReview();
    if (!current) return;

    const reason = rejectReason().trim() || '人工审核驳回';

    setSubmitting(true);
    try {
      await rejectHumanReview({
        issue_id: current.id,
        issue_identifier: current.identifier,
        reason,
        comments: reviewFeedback().trim() || undefined,
        resume_after_rejection: true,
        resume_instruction: resumeInstruction().trim() || undefined,
      });
      sessionStore.setStatus('审核已驳回，代理正在同步反馈');
      closeReview();
      await loadReviews();
    } catch (e) {
      console.error('Reject review failed:', e);
      sessionStore.setStatus(e instanceof Error ? e.message : '审核驳回失败');
    } finally {
      setSubmitting(false);
    }
  };

  const removeReview = async (review: HumanReviewItem) => {
    setSubmitting(true);
    try {
      await deleteHumanReview({
        issue_id: review.id,
        issue_identifier: review.identifier,
      });
      if (selectedReview() && reviewKey(selectedReview() as HumanReviewItem) === reviewKey(review)) {
        closeReview();
      }
      sessionStore.setStatus('审核记录已删除');
      await loadReviews();
    } catch (e) {
      console.error('Delete review failed:', e);
      sessionStore.setStatus(e instanceof Error ? e.message : '删除审核记录失败');
    } finally {
      setSubmitting(false);
    }
  };

  const shouldPollReviews = () =>
    autoRefresh()
    && isActive()
    && !workspaceBrowserOpen()
    && (activeVisibleReviews().length > 0 || Boolean(selectedReview()));

  let focusSequence = 0;
  let handledFocusRequest: HumanReviewPanelProps['focusRequest'];
  onCleanup(() => { focusSequence++; });
  const focusReview = async (detail: {reviewId?:string;reviewIdentifier?:string}) => {
    if (!isActive()) return;
    const sequence = ++focusSequence;
    const sessionId = sessionStore.currentSessionId();
    setFocusError('');
    closeReview();
    await rebuildVisibleSessionIds({ includePersisted: true });
    const hasTarget = Boolean(detail.reviewId?.trim() || detail.reviewIdentifier?.trim());
    let queue;
    try { queue = hasTarget ? await getHumanReviewQueue('', 1, {...detail, sessionId: sessionId || undefined}) : null; }
    catch {
      if (sequence === focusSequence && sessionId === sessionStore.currentSessionId()) {
        setFocusError('审核详情加载失败，请重试；未执行任何批准操作。');
        setReviewPanelOpen(true);
      }
      return;
    }
    if (sequence !== focusSequence || !isActive() || sessionId !== sessionStore.currentSessionId()) return;
    handledFocusRequest = detail;
    const target = queue?.reviews[0];
    if (target && isVisibleForCurrentScope(target)) {
      setReviewPanelOpen(false);
      openReview(target);
    } else {
      openReviewPanel();
      if (hasTarget) setFocusError('未找到对应审核项，可能已删除、当前不可见或暂时无法加载。请刷新后重试。');
      void loadReviews({ includeConfig: true });
    }
  };
  createEffect(on(() => [isActive(), props.focusRequest, sessionStore.currentSessionId()] as const, ([active, request]) => {
    if (!active) { focusSequence++; return; }
    if (request && request !== handledFocusRequest) void focusReview(request);
  }));
  onMount(() => {
    const handleOpenHumanReview = (event: Event) => {
      const detail = (event as CustomEvent<{
        reviewId?: string;
        reviewIdentifier?: string;
      }>).detail || {};
      void focusReview(detail);
    };
    window.addEventListener('tcode-open-human-review', handleOpenHumanReview);
    onCleanup(() => {
      window.removeEventListener('tcode-open-human-review', handleOpenHumanReview);
    });
  });

  createEffect(() => {
    if (!shouldPollReviews()) return;
    const timer = window.setInterval(() => {
      void loadReviews({ silent: true });
    }, REVIEW_POLL_INTERVAL_MS);
    onCleanup(() => window.clearInterval(timer));
  });

  createEffect(on(() => sessionStore.subagentsVersion(), () => {
    if (!isActive()) return;
    void rebuildVisibleSessionIds({ includePersisted: true });
  }, { defer: true }));

  createEffect(on(() => [isActive(), sessionStore.currentSessionId()] as const, ([active, sid]) => {
    queueRevision++;
    if (!active) return;
    setLoading(false);
    setQueueLoaded(false); setQueueError('');
    // Session switched, refresh review list for that session.
    const current = selectedReview();
    if (current) {
      if (!sid || !isVisibleForCurrentScope(current)) {
        closeReview();
      }
    }
    void rebuildVisibleSessionIds({ includePersisted: true });
    void loadReviews({ silent: true, includeConfig: !configLoaded() });
  }));

  const panelButtonClass = 'rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]';
  const neutralButtonClass = 'rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1.5 text-[11px] font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-50';
  const dangerButtonClass = 'rounded-full border border-[#ff9c96]/28 bg-[#ff9c96]/12 px-2.5 py-1.5 text-[11px] font-semibold text-[#ffd0cb] disabled:opacity-50';
  const successButtonClass = 'rounded-full border border-[#38b46a]/28 bg-[#38b46a]/12 px-3 py-1.5 text-xs font-semibold text-[#9ee6bb] disabled:opacity-50';

  return (
    <div class="mb-4 overflow-hidden rounded-3xl border border-[#2a3136] bg-[#13191d] shadow-[0_14px_34px_rgba(0,0,0,0.16)]">
      <div class="border-b border-[#2a3136] bg-[#10161a] px-4 py-4">
        <div class="flex items-center justify-between gap-3">
          <div class="min-w-0">
            <div class="flex min-w-0 items-center gap-2">
              <span class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#ffc176]">人工审核</span>
              <Show when={enabled()} fallback={<span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2.5 py-1 text-[10px] text-[#8b949e]">关闭</span>}>
                <span class="rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/12 px-2.5 py-1 text-[10px] text-[#8ed5ff]">开启</span>
              </Show>
              <button
                class={`rounded-full border px-2.5 py-1 text-[10px] transition-colors ${pendingVisibleReviews().length > 0 ? 'border-[#ff9c96]/28 bg-[#ff9c96]/12 text-[#ffd0cb] hover:border-[#ff9c96]/45' : 'border-[#3e484f] bg-[#171c20] text-[#8b949e] hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]'}`}
                onClick={openReviewPanel}
              >
                {queueError() ? '审核状态未知' : !queueLoaded() ? '审核加载中…' : `待审: ${pendingVisibleReviews().length}`}
              </button>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <button
              class={`${panelButtonClass} ${autoRefresh() ? 'border-[#38b46a]/28 bg-[#38b46a]/12 text-[#9ee6bb]' : ''}`}
              onClick={() => setAutoRefresh((prev) => !prev)}
              title="切换自动刷新"
            >
              {autoRefresh() ? '自动刷新 开' : '自动刷新 关'}
            </button>
            <button
              class={panelButtonClass}
              onClick={() => {
                void rebuildVisibleSessionIds({ includePersisted: true });
                void loadReviews({ includeConfig: true });
              }}
              disabled={loading()}
            >
              {loading() ? '刷新中...' : '刷新'}
            </button>
            <button class={neutralButtonClass} onClick={openReviewPanel}>
              打开列表
            </button>
          </div>
        </div>
      </div>

      <Show when={queueError()}><p role="alert" class="px-4 py-3 text-sm text-[#ffb6b0]">{queueError()}<button type="button" class="ml-2 underline" onClick={() => void loadReviews()}>重试加载</button></p></Show>

      <Show when={reviewPanelOpen() && isActive()}>
        <Portal>
        <div class="fixed inset-0 z-[94] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" onClick={closeReviewPanel}>
          <div
            class="flex h-[min(88vh,900px)] w-full max-w-[1500px] flex-col overflow-hidden rounded-[28px] border border-[#2a3136] bg-[#11171b] shadow-[0_30px_90px_rgba(0,0,0,0.5)]"
            onClick={(e) => e.stopPropagation()}
          >
            <Show when={focusError()}><p role="alert" class="px-5 pt-4 text-sm text-[#ffb6b0]">{focusError()}</p></Show>
            <div class="flex flex-wrap items-center justify-between gap-3 border-b border-[#2a3136] px-5 py-4">
              <div>
                <div class="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#ffc176]">人工审核</div>
                <div class="mt-1 text-sm font-semibold text-[#f5f7fa]">审核流程列表</div>
              </div>
              <div class="flex items-center gap-2">
                <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#9faeb7]">{visibleReviews().length} 项</span>
                <button
                  class={panelButtonClass}
                  onClick={() => {
                    void rebuildVisibleSessionIds({ includePersisted: true });
                    void loadReviews({ includeConfig: true });
                  }}
                  disabled={loading()}
                >
                  {loading() ? '刷新中...' : '刷新'}
                </button>
                <button class={neutralButtonClass} onClick={closeReviewPanel}>关闭</button>
              </div>
            </div>

            <div class="flex flex-wrap items-center justify-between gap-3 border-b border-[#2a3136] bg-[#10161a] px-5 py-3">
              <div class="text-[11px] text-[#6f7d87]">
                当前 Session 审核溯源
                <Show when={lastUpdatedAt()}> · 更新于 {lastUpdatedAt()}</Show>
              </div>
              <div class="flex flex-wrap items-center gap-2">
                <For each={REVIEW_FLOW_COLUMNS}>
                  {(column) => (
                    <span class={`rounded-full border px-2.5 py-1 text-[10px] ${column.badge}`}>
                      {column.label}: {visibleReviewsByFlow().get(column.status)?.length || 0}
                    </span>
                  )}
                </For>
              </div>
            </div>

            <div class="tcode-scrollbar min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-4">
              <Show when={visibleReviews().length > 0} fallback={<div class="rounded-2xl border border-dashed border-[#3e484f] bg-[#171c20] px-4 py-6 text-sm text-[#8b949e]">当前会话无审核记录</div>}>
                <div class="grid h-full min-w-[900px] grid-cols-4 gap-3">
                  <For each={REVIEW_FLOW_COLUMNS}>
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
                            {visibleReviewsByFlow().get(column.status)?.length || 0}
                          </span>
                        </div>

                        <div class="tcode-scrollbar min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
                          <Show when={(visibleReviewsByFlow().get(column.status)?.length || 0) > 0} fallback={<div class="rounded-2xl border border-dashed border-[#303539] px-4 py-8 text-center text-xs text-[#6f7d87]">{column.empty}</div>}>
                            <For each={visibleReviewsByFlow().get(column.status) || []}>
                              {(review) => (
                                <article class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#171c20] shadow-[0_12px_28px_rgba(0,0,0,0.16)]">
                                  <button
                                    type="button"
                                    class="block w-full px-3.5 py-3 text-left transition-colors hover:bg-[#1a2126]"
                                    onClick={() => toggleExpandedReview(review)}
                                  >
                                    <div class="flex items-start justify-between gap-2">
                                      <span class="min-w-0 truncate text-[10px] text-[#7bd0ff]">{isGoalReview(review) ? '执行前审核' : '结果与操作审核'}</span>
                                      <span class={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${column.badge}`}>
                                        {reviewFlowLabel(review)}
                                      </span>
                                    </div>
                                    <div class="mt-2 line-clamp-2 text-sm font-semibold leading-5 text-[#f1f5f8]">{toolDisplayLabel(review.tool_name)}</div>
                                    <div class="mt-2 line-clamp-2 text-[11px] leading-4 text-[#9faeb7]">{review.reason || '需要人工确认'}</div>
                                    <div class="mt-3 flex items-center justify-between gap-2 text-[10px] text-[#6f7d87]">
                                      <span class="truncate" title={reviewWorkspaceLabel(review)}>Workspace: {reviewWorkspaceLabel(review)}</span>
                                      <span>{formatReviewTimestamp(review.updated_at || review.created_at)}</span>
                                    </div>
                                    <Show when={review.resume_status}>
                                      <div class="mt-2 text-[10px] text-[#8b949e]">恢复: {resumeStatusLabel(String(review.resume_status || ''))}</div>
                                    </Show>
                                    <div class="mt-2 flex items-center gap-1.5 text-[10px] text-[#8b949e]">
                                      <svg xmlns="http://www.w3.org/2000/svg" class={`h-3 w-3 transition-transform ${expandedReviewKey() === reviewKey(review) ? 'rotate-90' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                                      </svg>
                                      {expandedReviewKey() === reviewKey(review) ? '收起详情' : '展开详情'}
                                    </div>
                                  </button>

                                  <Show when={expandedReviewKey() === reviewKey(review)}>
                                    <div class="space-y-3 border-t border-[#2a3136] bg-[#13191d] px-3.5 py-3">
                                      <div class="grid gap-2">
                                        <For each={reviewSummaryRows(review)}>
                                          {(row) => (
                                            <div class="rounded-xl border border-[#2a3136] bg-[#10161a] px-3 py-2">
                                              <div class="text-[10px] text-[#6f7d87]">{row.label}</div>
                                              <div class="mt-1 break-words text-[11px] text-[#d5dde3]">{row.value}</div>
                                            </div>
                                          )}
                                        </For>
                                      </div>

                                      <div>
                                        <div class="mb-1.5 text-[10px] text-[#8b949e]">触发原因</div>
                                        <div class="tcode-scrollbar max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[#2a3136] bg-[#10161a] p-3 text-[11px] text-[#c9d1d9]">
                                          {review.reason || '需要人工确认'}
                                        </div>
                                      </div>

                                      <Show when={!isGoalReview(review)}><div>
                                        <div class="mb-1.5 text-[10px] text-[#8b949e]">工具参数</div>
                                        <pre class="tcode-scrollbar max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[#2a3136] bg-[#0d1216] p-3 text-[11px] text-[#c9dfe7]">{JSON.stringify(review.tool_args || {}, null, 2)}</pre>
                                      </div>

                                      <JobWorkspacePanel
                                        workspace={reviewWorkspace(review)}
                                        title="任务工作区"
                                        onBrowserOpenChange={setWorkspaceBrowserOpen}
                                        onActionComplete={() => {
                                          void loadReviews();
                                        }}
                                      />
                                      </Show>

                                      <div class="flex flex-wrap items-center gap-2 pt-1">
                                        <Show when={reviewFlowStatus(review) === 'todo'}>
                                          <button class={neutralButtonClass} onClick={() => openReview(review)}>审核处理</button>
                                        </Show>
                                        <button
                                          class={dangerButtonClass}
                                          onClick={() => void removeReview(review)}
                                          disabled={submitting()}
                                        >
                                          删除
                                        </button>
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
              </Show>
            </div>
          </div>
        </div>
        </Portal>
      </Show>

      <Show when={selectedReview() && isActive()}>
        <Portal>
        <div class="fixed inset-0 z-[95] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" onClick={closeReview}>
          <div
            role="dialog" aria-modal="true" aria-label="人工审核详情"
            class="max-h-[90vh] w-full max-w-4xl overflow-hidden rounded-[28px] border border-[#2a3136] bg-[#11171b] shadow-[0_28px_80px_rgba(0,0,0,0.46)]"
            onClick={(e) => e.stopPropagation()}
          >
            <div class="flex items-center justify-between border-b border-[#2a3136] bg-[#10161a] px-5 py-4">
              <div class="min-w-0">
                <div class="truncate text-xs text-[#8b949e]">{isGoalReview(selectedReview()) ? '执行前审核 · 仅本次计算' : '人工审核详情'}</div>
                <div class="truncate text-sm font-semibold text-[#e6edf3]">{toolDisplayLabel(selectedReview()?.tool_name)}</div>
              </div>
              <button class={neutralButtonClass} onClick={closeReview}>关闭</button>
            </div>

            <div class="tcode-scrollbar max-h-[calc(90vh-58px)] space-y-4 overflow-auto p-5">
              <Show when={isGoalReview(selectedReview())}>
                <p role="status" class="text-sm">{selectedReview() ? reviewPhase(selectedReview()!) : ''}</p>
                <p class="text-sm">仅批准所列输入和参数的一次计算，不包含重跑或后续任务。时长与资源成本未确认；拒绝后不会提交该计算。</p>
                <dl class="grid grid-cols-2 gap-2 text-sm"><For each={selectedReview() ? reviewSummary(selectedReview()!) : []}>{row => <><dt>{row.label}</dt><dd>{row.value}</dd></>}</For></dl>
                <Show when={selectedReview()?.resume_status === 'failed'}><p role="alert" class="text-sm text-[#ffb6b0]">已批准，但提交或恢复失败；不代表计算成功，也不会自动重提。请保留本记录核查后再决定是否重新申请。</p></Show>
              </Show>
              <div>
                <div class="mb-2 text-xs text-[#8b949e]">触发原因</div>
                <div class="rounded-2xl border border-[#2a3136] bg-[#171c20] p-3 text-sm text-[#e6edf3] whitespace-pre-wrap break-words">
                  {selectedReview()?.reason || '需要人工确认'}
                </div>
              </div>

              <Show when={!isGoalReview(selectedReview())}><div>
                <div class="mb-2 text-xs text-[#8b949e]">工具参数</div>
                <pre class="rounded-2xl border border-[#2a3136] bg-[#0d1216] p-4 text-xs text-[#c9dfe7] whitespace-pre-wrap break-words">{JSON.stringify(selectedReview()?.tool_args || {}, null, 2)}</pre>
              </div>

              <JobWorkspacePanel
                workspace={reviewWorkspace(selectedReview())}
                title="任务工作区"
                onBrowserOpenChange={setWorkspaceBrowserOpen}
                onActionComplete={() => {
                  void loadReviews();
                }}
              />
              </Show>

              <div>
                <div class="mb-2 text-xs text-[#8b949e]">审核反馈</div>
                <textarea
                  class="textarea textarea-sm w-full bg-[#0f1418] border-[#2a3136] text-[#e6edf3]"
                  rows={4}
                  value={reviewFeedback()}
                  onInput={(e) => setReviewFeedback(e.currentTarget.value)}
                  placeholder="填写后原调用不会立即执行，代理将先按反馈修正；仅确认运行时请留空"
                />
              </div>

              <div>
                <div class="mb-2 text-xs text-[#8b949e]">继续执行指令</div>
                <textarea
                  class="textarea textarea-sm w-full bg-[#0f1418] border-[#2a3136] text-[#e6edf3]"
                  rows={3}
                  value={resumeInstruction()}
                  onInput={(e) => setResumeInstruction(e.currentTarget.value)}
                  placeholder="例如：先重跑第二步并使用更保守参数，再继续后续步骤"
                />
              </div>

              <Show when={!isGoalReview(selectedReview())}><div>
                <div class="mb-2 text-xs text-[#8b949e]">参数覆盖（可选 JSON 对象）</div>
                <textarea
                  class="textarea textarea-sm w-full bg-[#0f1418] border-[#2a3136] text-[#e6edf3] font-mono"
                  rows={6}
                  value={overrideJson()}
                  onInput={(e) => setOverrideJson(e.currentTarget.value)}
                  placeholder={'填写后将按覆盖参数直接执行，例如: {"input_config": {"sampling_steps": 100}}'}
                />
              </div>
              </Show>

              <div>
                <div class="mb-2 text-xs text-[#8b949e]">驳回原因 (仅驳回时必填)</div>
                <input
                  class="input input-sm w-full bg-[#0f1418] border-[#2a3136] text-[#e6edf3]"
                  value={rejectReason()}
                  onInput={(e) => setRejectReason(e.currentTarget.value)}
                  placeholder="例如：参数风险过高，请先缩小搜索范围"
                />
              </div>

              <div class="flex items-center justify-end gap-2 border-t border-[#2a3136] pt-3">
                <button
                  class={dangerButtonClass}
                  onClick={() => {
                    const review = selectedReview();
                    if (review) void removeReview(review);
                  }}
                  disabled={submitting()}
                >
                  {submitting() ? '提交中...' : '删除记录'}
                </button>
                <button
                  class={dangerButtonClass}
                  onClick={() => void reject()}
                  disabled={submitting() || selectedReview()?.status !== 'pending_review'}
                >
                  {submitting() ? '提交中...' : '驳回'}
                </button>
                <button
                  class={successButtonClass}
                  onClick={() => void approve()}
                  disabled={submitting() || selectedReview()?.status !== 'pending_review'}
                >
                  {submitting() ? '提交中...' : isGoalReview(selectedReview()) ? (reviewFeedback().trim() || resumeInstruction().trim() ? '要求修改后重新审核' : '批准本次计算') : '通过并继续执行'}
                </button>
              </div>
            </div>
          </div>
        </div>
        </Portal>
      </Show>
    </div>
  );
}
