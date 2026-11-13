import { confirmAction } from './FeedbackDialog';
import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
} from 'solid-js';
import {
  deleteKanbanCard,
  getKanban,
  getSymphonyLocalIssues,
  type KanbanCard,
  type KanbanStatus,
} from '../api/client';
import { sessionStore } from '../stores/session';
import { createPageVisibleSignal } from '../utils/pageVisibility';
import { formatAsyncResultSummary } from '../utils/asyncResultSummary';
import { bridgeWorkerWarning } from '../utils/bridgeWorkerHealth';
import JobWorkspacePanel from './JobWorkspacePanel';
import { openCardDetail } from '../utils/cardTransition';

type KanbanColumn = {
  status: KanbanStatus;
  label: string;
  accent: string;
  dot: string;
};

const COLUMNS: KanbanColumn[] = [
  { status: 'todo', label: '待处理', accent: 'text-[#ffe1b3]', dot: 'bg-[#f1a02b]' },
  { status: 'in_progress', label: '进行中', accent: 'text-[#8ed5ff]', dot: 'bg-[#38bdf8]' },
  { status: 'completed', label: '已完成', accent: 'text-[#9ee6bb]', dot: 'bg-[#38b46a]' },
  { status: 'failed', label: '失败', accent: 'text-[#ffb6b0]', dot: 'bg-[#da3633]' },
  { status: 'cancelled', label: '已取消', accent: 'text-[#bdc8d1]', dot: 'bg-[#6f7d87]' },
];

const TERMINAL_STATUSES = new Set<KanbanStatus>(['completed', 'failed', 'cancelled']);
type KanbanFilterMode = 'session' | 'workspace' | 'all';

type KanbanWorkspaceProps = {
  onOpenHumanReview?: (card: KanbanCard) => void;
};

function formatTime(value?: string) {
  const text = String(value || '').trim();
  if (!text) return '-';
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? text : date.toLocaleString();
}

function sourceLabel(card: KanbanCard) {
  const labels: Record<string, string> = {
    todo: 'Todo',
    subagent: '子 Agent',
    async: '异步任务',
  };
  const sources = Array.isArray(card.sources) && card.sources.length > 1
    ? card.sources
    : [card.source_type];
  return sources.map((source) => labels[source] || source).join(' + ');
}

function cardBorder(status: KanbanStatus) {
  if (status === 'in_progress') return 'border-[#38bdf8]/34';
  if (status === 'completed') return 'border-[#38b46a]/30';
  if (status === 'failed') return 'border-[#da3633]/36';
  if (status === 'cancelled') return 'border-[#59636a]/36';
  return 'border-[#f1a02b]/30';
}

function progressColor(status: KanbanStatus) {
  if (status === 'completed') return 'bg-[#38b46a]';
  if (status === 'failed') return 'bg-[#da3633]';
  if (status === 'cancelled') return 'bg-[#6f7d87]';
  if (status === 'in_progress') return 'bg-[#38bdf8]';
  return 'bg-[#f1a02b]';
}

export default function KanbanWorkspace(props: KanbanWorkspaceProps = {}) {
  const [cards, setCards] = createSignal<KanbanCard[]>([]);
  const [loading, setLoading] = createSignal(true);
  const [error, setError] = createSignal('');
  const [lastUpdatedAt, setLastUpdatedAt] = createSignal('');
  const [pollIntervalSeconds, setPollIntervalSeconds] = createSignal(8);
  const [selectedCard, setSelectedCard] = createSignal<KanbanCard | null>(null);
  const [deletingCardId, setDeletingCardId] = createSignal('');
  const [filterMode, setFilterMode] = createSignal<KanbanFilterMode>('workspace');
  const [workspacePanelOpen, setWorkspacePanelOpen] = createSignal(false);
  const [loadingCardDetail, setLoadingCardDetail] = createSignal(false);
  const pageVisible = createPageVisibleSignal();

  const currentWorkspaceId = () => {
    const session = sessionStore.currentSession();
    const explicit = String(session?.workdir_name || '').trim();
    if (explicit) return explicit;
    const workdir = String(session?.workdir || '').trim().replace(/\/+$/, '');
    return workdir ? workdir.split('/').pop() || '' : '';
  };

  const activeFilters = () => ({
    workspaceId: filterMode() === 'workspace' ? currentWorkspaceId() : '',
    sessionId: filterMode() === 'session'
      ? String(sessionStore.currentSessionId() || '').trim()
      : '',
  });

  const openCard = (card: KanbanCard) => {
    setWorkspacePanelOpen(false);
    setSelectedCard(card);
    if (card.source_type !== 'async') return;
    const sessionIds = [card.session_id, card.root_session_id]
      .map((item) => String(item || '').trim())
      .filter(Boolean);
    setLoadingCardDetail(true);
    void getSymphonyLocalIssues(
      500,
      String(card.session_id || card.root_session_id || '').trim(),
      {
        sessionIds,
        workspaceId: card.workspace_id,
      },
    ).then((data) => {
      const issue = (data.issues || []).find((item) => (
        (card.async_task_id && item.id === card.async_task_id)
        || (
          card.issue_identifier
          && item.identifier === card.issue_identifier
        )
      ));
      if (!issue) return;
      setSelectedCard((current) => (
        current?.id === card.id
          ? {
              ...current,
              workspace: {
                ...(current.workspace || {}),
                ...(issue.workspace || {}),
                timeline: Array.isArray(issue.timeline)
                  ? issue.timeline
                  : issue.workspace?.timeline,
              },
              result_summary: issue.result_summary || current.result_summary,
              error_summary: issue.error_summary || current.error_summary,
            }
          : current
      ));
    }).catch(() => {
      // Kanban payload already contains a usable workspace fallback.
    }).finally(() => {
      setLoadingCardDetail(false);
    });
  };

  const closeCard = () => {
    setLoadingCardDetail(false);
    setWorkspacePanelOpen(false);
    setSelectedCard(null);
  };

  const cardsByStatus = createMemo(() => {
    const grouped = new Map<KanbanStatus, KanbanCard[]>();
    for (const column of COLUMNS) grouped.set(column.status, []);
    for (const card of cards()) {
      grouped.get(card.status)?.push(card);
    }
    return grouped;
  });
  const selectedResultView = createMemo(() =>
    formatAsyncResultSummary(
      selectedCard()?.result_summary,
      selectedCard()?.error_summary,
    )
  );

  const loadCards = async (silent = false) => {
    if (!pageVisible()) return;
    if (!silent) setLoading(true);
    setError('');
    try {
      const data = await getKanban(activeFilters());
      const nextCards = Array.isArray(data.cards) ? data.cards : [];
      setCards(nextCards);
      setSelectedCard((current) => {
        if (!current) return null;
        const refreshed = nextCards.find((item) => item.id === current.id);
        if (!refreshed) return null;
        // Async card payloads intentionally cap summaries for list performance.
        // Keep the richer issue detail already fetched by openCard instead of
        // replacing it with a truncated JSON string on every poll.
        const currentResult = String(current.result_summary || '');
        const refreshedResult = String(refreshed.result_summary || '');
        const currentError = String(current.error_summary || '');
        const refreshedError = String(refreshed.error_summary || '');
        return {
          ...current,
          ...refreshed,
          result_summary: currentResult.length > refreshedResult.length
            ? currentResult
            : refreshedResult,
          error_summary: currentError.length > refreshedError.length
            ? currentError
            : refreshedError,
          workspace: {
            ...(refreshed.workspace || {}),
            ...(current.workspace || {}),
          },
        };
      });
      setPollIntervalSeconds(Math.max(2, Number(data.poll_interval_seconds || 8)));
      setLastUpdatedAt(new Date().toLocaleTimeString());
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载任务看板失败');
    } finally {
      if (!silent) setLoading(false);
    }
  };

  const removeCard = async (card: KanbanCard) => {
    if (!TERMINAL_STATUSES.has(card.status)) return;
    const confirmed = await confirmAction(`确认从看板删除“${card.title}”吗？任务执行记录仍会保留。`);
    if (!confirmed) return;
    setDeletingCardId(card.id);
    try {
      await deleteKanbanCard(card.id);
      setCards((items) => items.filter((item) => item.id !== card.id));
      if (selectedCard()?.id === card.id) closeCard();
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除任务卡片失败');
    } finally {
      setDeletingCardId('');
    }
  };

  createEffect(() => {
    filterMode();
    sessionStore.currentSessionId();
    currentWorkspaceId();
    pageVisible();
    void loadCards();
  });

  createEffect(() => {
    if (!pageVisible()) return;
    const interval = window.setInterval(() => {
      void loadCards(true);
    }, pollIntervalSeconds() * 1000);
    onCleanup(() => window.clearInterval(interval));
  });

  return (
    <section class="flex h-full min-w-0 flex-1 flex-col bg-[#0f1418]/82">
      <div class="flex flex-wrap items-center justify-between gap-4 border-b border-[#2a3136] bg-[#0d1216]/92 px-5 py-4">
        <div>
          <div class="text-[11px] font-semibold uppercase tracking-[0.22em] text-[#7bd0ff]">Agent Progress</div>
          <h1 class="mt-1 text-xl font-semibold text-[#f7fafc]">任务看板</h1>
          <p class="mt-1 text-sm text-[#9faeb7]">按 Session、Workspace 或当前用户可见范围汇总 Todo、子 Agent 与异步任务。</p>
        </div>
        <div class="flex items-center gap-2 text-xs">
          <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[#9faeb7]">
            {cards().length} 个任务
          </span>
          <span class="hidden rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-[#7f8b93] sm:inline-flex">
            {lastUpdatedAt() ? `更新于 ${lastUpdatedAt()}` : `每 ${pollIntervalSeconds()} 秒刷新`}
          </span>
          <button
            type="button"
            class="inline-flex h-8 items-center justify-center rounded-full border border-[#7bd0ff]/30 bg-[#38bdf8]/10 px-3 font-semibold text-[#8ed5ff] transition-colors hover:bg-[#38bdf8]/16 disabled:opacity-60"
            onClick={() => void loadCards()}
            disabled={loading()}
          >
            {loading() ? '刷新中...' : '刷新'}
          </button>
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
                  filterMode() === filter.value
                    ? 'border-[#7bd0ff]/35 bg-[#38bdf8]/12 text-[#8ed5ff]'
                    : 'border-[#3e484f] bg-[#171c20] text-[#8b949e] hover:border-[#7bd0ff]/25 hover:text-[#bdc8d1]'
                }`}
                onClick={() => setFilterMode(filter.value)}
                disabled={
                  (filter.value === 'session' && !sessionStore.currentSessionId())
                  || (filter.value === 'workspace' && !currentWorkspaceId())
                }
              >
                {filter.label}
              </button>
            )}
          </For>
        </div>
        <div class="text-[11px] text-[#6f7d87]">
          <Show when={filterMode() === 'session'}>Session: {sessionStore.currentSessionId() || '-'}</Show>
          <Show when={filterMode() === 'workspace'}>Workspace: {currentWorkspaceId() || '-'}</Show>
          <Show when={filterMode() === 'all'}>当前用户全部可见任务</Show>
        </div>
      </div>

      <Show when={error()}>
        <div class="mx-5 mt-4 rounded-2xl border border-[#da3633]/35 bg-[#da3633]/10 px-4 py-3 text-sm text-[#ffb6b0]">
          {error()}
        </div>
      </Show>

      <div class="tcode-scrollbar min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-4">
        <div class="grid h-full min-w-[1420px] grid-cols-5 gap-3">
          <For each={COLUMNS}>
            {(column) => (
              <div class="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-[#2a3136] bg-[#11171b]/92">
                <div class="flex items-center justify-between border-b border-[#2a3136] bg-[#151b1f] px-4 py-3">
                  <div class={`flex items-center gap-2 text-sm font-semibold ${column.accent}`}>
                    <span class={`h-2.5 w-2.5 rounded-full ${column.dot}`} />
                    {column.label}
                  </div>
                  <span class="rounded-full border border-[#3e484f] bg-[#0f1418] px-2.5 py-0.5 text-[11px] text-[#9faeb7]">
                    {cardsByStatus().get(column.status)?.length || 0}
                  </span>
                </div>

                <div class="tcode-scrollbar min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
                  <Show
                    when={(cardsByStatus().get(column.status)?.length || 0) > 0}
                    fallback={
                      <div class="rounded-2xl border border-dashed border-[#303539] px-4 py-8 text-center text-xs text-[#6f7d87]">
                        暂无任务
                      </div>
                    }
                  >
                    <For each={cardsByStatus().get(column.status) || []}>
                      {(card) => (
                        <article
                          class={`rounded-2xl border bg-[#171c20] p-3.5 shadow-[0_12px_30px_rgba(0,0,0,0.18)] transition-colors hover:bg-[#1a2126] ${cardBorder(card.status)}`}
                        >
                          <button
                            type="button"
                            class="block w-full text-left"
                            onClick={(event) => openCardDetail(event.currentTarget.closest('article') as HTMLElement, '.task-card-detail', () => openCard(card))}
                          >
                            <div class="flex items-start justify-between gap-2">
                              <span class="rounded-full border border-[#3e484f] bg-[#0f1418] px-2 py-0.5 text-[10px] font-semibold text-[#9faeb7]">
                                {sourceLabel(card)}
                              </span>
                              <span class="max-w-[120px] truncate font-mono text-[10px] text-[#6f7d87]" title={card.workspace_id}>
                                {card.workspace_id || '默认项目'}
                              </span>
                            </div>
                            <h2 class="mt-3 line-clamp-3 text-sm font-semibold leading-5 text-[#f1f5f8]">
                              {card.title}
                            </h2>
                            <div class="mt-3 text-[11px] text-[#9faeb7]">
                              执行 Agent: <span class="text-[#cbd5dc]">{card.agent || '-'}</span>
                            </div>
                            <div class="mt-3">
                              <div class="mb-1.5 flex items-center justify-between text-[10px] text-[#8b949e]">
                                <span>{card.progress_label}</span>
                                <span>{Math.max(0, Math.min(100, Number(card.progress_percent || 0)))}%</span>
                              </div>
                              <div class="h-1.5 overflow-hidden rounded-full bg-[#0d1216]">
                                <div
                                  class={`h-full rounded-full ${progressColor(card.status)}`}
                                  style={{ width: `${Math.max(0, Math.min(100, Number(card.progress_percent || 0)))}%` }}
                                />
                              </div>
                            </div>
                            <Show when={card.current_step}>
                              <div class="mt-3 rounded-xl border border-[#2a3136] bg-[#10161a] px-3 py-2">
                                <div class="text-[10px] text-[#6f7d87]">当前步骤</div>
                                <div class="mt-1 line-clamp-2 text-[11px] leading-4 text-[#bdc8d1]">{card.current_step}</div>
                              </div>
                            </Show>
                            <Show when={bridgeWorkerWarning(card)}>
                              <div class="mt-3 rounded-xl border border-[#f1a02b]/35 bg-[#f1a02b]/10 px-3 py-2 text-[11px] leading-4 text-[#ffc176]">
                                {bridgeWorkerWarning(card)}
                              </div>
                            </Show>
                            <div class="mt-3 flex items-center justify-between text-[10px] text-[#6f7d87]">
                              <span>创建 {formatTime(card.created_at)}</span>
                            </div>
                            <div class="mt-1 text-[10px] text-[#6f7d87]">
                              更新 {formatTime(card.updated_at)}
                            </div>
                          </button>
                          <Show when={TERMINAL_STATUSES.has(card.status)}>
                            <div class="mt-3 border-t border-[#2a3136] pt-3">
                              <button
                                type="button"
                                class="text-[11px] font-semibold text-[#ff9c96] transition-colors hover:text-[#ffd0cb] disabled:opacity-50"
                                disabled={deletingCardId() === card.id}
                                onClick={() => void removeCard(card)}
                              >
                                {deletingCardId() === card.id ? '删除中...' : '从看板删除'}
                              </button>
                            </div>
                          </Show>
                        </article>
                      )}
                    </For>
                  </Show>
                </div>
              </div>
            )}
          </For>
        </div>
      </div>

      <Show when={selectedCard()}>
        <div
          class="fixed inset-0 z-[95] flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm"
          onClick={closeCard}
        >
          <div
            class="task-card-detail flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-3xl border border-[#2a3136] bg-[#11171b] shadow-[0_30px_90px_rgba(0,0,0,0.5)]"
            onClick={(event) => event.stopPropagation()}
          >
            <div class="flex items-start justify-between gap-4 border-b border-[#2a3136] px-5 py-4">
              <div class="min-w-0">
                <div class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#7bd0ff]">
                  {selectedCard() ? sourceLabel(selectedCard() as KanbanCard) : ''}
                </div>
                <div class="mt-2 text-lg font-semibold text-[#f7fafc]">{selectedCard()?.title}</div>
                <Show when={loadingCardDetail()}>
                  <div class="mt-2 text-[11px] text-[#8b949e]">正在同步最新任务详情...</div>
                </Show>
              </div>
              <button
                type="button"
                class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1.5 text-xs font-semibold text-[#bdc8d1]"
                onClick={closeCard}
              >
                关闭
              </button>
            </div>
            <div class="tcode-scrollbar space-y-4 overflow-y-auto p-5">
              <div class="grid gap-3 sm:grid-cols-2">
                <For
                  each={[
                    ['执行 Agent', selectedCard()?.agent || '-'],
                    ['项目', selectedCard()?.workspace_id || '-'],
                    ['Session', selectedCard()?.session_id || '-'],
                    ['创建时间', formatTime(selectedCard()?.created_at)],
                    ['更新时间', formatTime(selectedCard()?.updated_at)],
                    ['进度', `${selectedCard()?.progress_label || '-'} · ${selectedCard()?.progress_percent || 0}%`],
                    ['当前步骤', selectedCard()?.current_step || '-'],
                  ]}
                >
                  {(row) => (
                    <div class="rounded-2xl border border-[#2a3136] bg-[#171c20] px-3 py-2.5">
                      <div class="text-[10px] text-[#6f7d87]">{row[0]}</div>
                      <div class="mt-1 break-words text-xs text-[#d5dde3]">{row[1]}</div>
                    </div>
                  )}
                </For>
              </div>
              <Show when={selectedCard()?.result_summary}>
                <div>
                  <div class="mb-2 text-xs font-semibold text-[#9ee6bb]">结果摘要</div>
                  <pre class="whitespace-pre-wrap break-words rounded-2xl border border-[#238636]/30 bg-[#0f1913] p-4 text-xs leading-5 text-[#b7e9c9]">{selectedResultView().text}</pre>

                </div>
              </Show>
              <Show when={selectedCard()?.error_summary}>
                <div>
                  <div class="mb-2 text-xs font-semibold text-[#ffb6b0]">错误摘要</div>
                  <pre class="whitespace-pre-wrap break-words rounded-2xl border border-[#da3633]/30 bg-[#201317] p-4 text-xs leading-5 text-[#ffb6b0]">{selectedCard()?.error_summary}</pre>
                </div>
              </Show>
              <Show when={selectedCard() && bridgeWorkerWarning(selectedCard() as KanbanCard)}>
                <div class="rounded-2xl border border-[#f1a02b]/35 bg-[#f1a02b]/10 p-4 text-xs leading-5 text-[#ffc176]">
                  <div class="font-semibold">Bridge Worker 未接单</div>
                  <div class="mt-1">{bridgeWorkerWarning(selectedCard() as KanbanCard)}</div>
                </div>
              </Show>
              <Show when={selectedCard()?.human_review}>
                <div class="rounded-2xl border border-[#f1a02b]/28 bg-[#f1a02b]/8 p-4">
                  <div class="text-xs font-semibold text-[#ffe1b3]">等待人工审核</div>
                  <div class="mt-2 text-xs leading-5 text-[#bdc8d1]">
                    {selectedCard()?.human_review?.reason || '该任务需要人工确认后继续执行。'}
                  </div>
                  <div class="mt-3 flex flex-wrap items-center gap-2">
                    <span class="font-mono text-[10px] text-[#8b949e]">
                      {selectedCard()?.human_review?.identifier || selectedCard()?.human_review?.id}
                    </span>
                    <button
                      type="button"
                      class="rounded-full border border-[#f1a02b]/30 bg-[#f1a02b]/12 px-4 py-2 text-xs font-semibold text-[#ffe1b3]"
                      onClick={() => {
                        const card = selectedCard();
                        if (card) props.onOpenHumanReview?.(card);
                      }}
                    >
                      进入人审
                    </button>
                  </div>
                </div>
              </Show>
              <Show when={selectedCard()?.workspace && Object.keys(selectedCard()?.workspace || {}).length > 0}>
                <div class="border-t border-[#2a3136] pt-4">
                  <button
                    type="button"
                    class="rounded-full border border-[#38bdf8]/28 bg-[#38bdf8]/12 px-4 py-2 text-xs font-semibold text-[#8ed5ff]"
                    onClick={() => setWorkspacePanelOpen((open) => !open)}
                  >
                    {workspacePanelOpen() ? '收起任务工作区' : '进入任务工作区'}
                  </button>
                </div>
                <Show when={workspacePanelOpen()}>
                  <div class="mt-4">
                    <JobWorkspacePanel
                      workspace={selectedCard()?.workspace}
                      title="任务工作区"
                      onActionComplete={() => void loadCards(true)}
                    />
                  </div>
                </Show>
              </Show>
              <Show when={selectedCard() && TERMINAL_STATUSES.has((selectedCard() as KanbanCard).status)}>
                <div class="border-t border-[#2a3136] pt-4">
                  <button
                    type="button"
                    class="rounded-full border border-[#ff9c96]/30 bg-[#ff9c96]/10 px-4 py-2 text-sm font-semibold text-[#ffd0cb]"
                    onClick={() => {
                      const card = selectedCard();
                      if (card) void removeCard(card);
                    }}
                    disabled={deletingCardId() === selectedCard()?.id}
                  >
                    {deletingCardId() === selectedCard()?.id ? '删除中...' : '从看板删除'}
                  </button>
                </div>
              </Show>
            </div>
          </div>
        </div>
      </Show>
    </section>
  );
}
