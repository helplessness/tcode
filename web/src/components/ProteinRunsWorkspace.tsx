import { confirmAction } from './FeedbackDialog';
import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from 'solid-js';
import {
  cancelProteinRunAttempt,
  deleteProteinRun,
  getProteinRun,
  listProteinRuns,
  proteinRunArtifactUrl,
  reconcileProteinRunAttempt,
  retryProteinRunAttempt,
  type ProteinRunDetail,
  type ProteinRunAttempt,
  type ProteinRunSummary,
  type ProteinRunWatcherHealth,
} from '../api/proteinRuns';
import JobActionControls from './JobActionControls';
import { formatAsyncResultSummary } from '../utils/asyncResultSummary';
import { userFacingTitle, toolDisplayLabel } from '../utils/userFacingLabels';

type ProteinRunsWorkspaceProps = {
  workspaceId?: string;
  active?: boolean;
  focusRequest?: {runId: string; key: number};
};

const panelClass = 'rounded-2xl border border-[#2a3136] bg-[#151b1f]/88 p-4';
const mutedLabelClass = 'text-[10px] font-semibold uppercase tracking-[0.2em] text-[#71808a]';

function formatTime(value?: string): string {
  const text = String(value || '').trim();
  if (!text) return '—';
  const date = new Date(text);
  return Number.isNaN(date.getTime())
    ? text
    : date.toLocaleString('zh-CN', { hour12: false });
}

function formatBytes(value?: number): string {
  const size = Math.max(0, Number(value || 0));
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function statusLabel(status?: string): string {
  const normalized = String(status || '').toLowerCase();
  return ({
    active: '运行中',
    running: '运行中',
    waiting_external: '等待后台计算',
    collecting: '正在回收结果',
    retry_wait: '等待重新同步',
    sync_failed: '结果同步失败',
    resumed: '已恢复',
    paused: '已暂停',
    awaiting_input: '等待输入',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    blocked: '已阻塞',
    retained: '结果已保留',
    unknown: '未知',
  } as Record<string, string>)[normalized] || normalized || '未知';
}

function statusClass(status?: string): string {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'completed') return 'border-[#38b46a]/35 bg-[#38b46a]/10 text-[#9ee6bb]';
  if (['active', 'resumed', 'running', 'waiting_external', 'collecting'].includes(normalized)) return 'border-[#38bdf8]/35 bg-[#38bdf8]/10 text-[#8ed5ff]';
  if (normalized === 'failed' || normalized === 'blocked') return 'border-[#ef4444]/35 bg-[#ef4444]/10 text-[#fca5a5]';
  if (normalized === 'sync_failed') return 'border-[#ef4444]/35 bg-[#ef4444]/10 text-[#fca5a5]';
  if (normalized === 'retry_wait') return 'border-[#f1a02b]/35 bg-[#f1a02b]/10 text-[#ffc176]';
  if (normalized === 'awaiting_input' || normalized === 'paused') return 'border-[#f1a02b]/35 bg-[#f1a02b]/10 text-[#ffc176]';
  return 'border-[#3e484f] bg-[#1b2024] text-[#bdc8d1]';
}

function recoveryLabel(state?: string): string {
  return ({
    not_needed: '任务已完成，无需恢复',
    checkpoint_available: '存在可恢复检查点',
    in_progress_or_interrupted: '运行中或可能异常中断',
    blocked_input_drift: '输入已变化，禁止直接恢复',
    checkpoint_present: '已记录检查点',
    unavailable: '没有可用检查点',
  } as Record<string, string>)[String(state || '')] || '恢复状态未知';
}

function compactHash(value?: string): string {
  const text = String(value || '').trim();
  if (!text) return '—';
  return text.length > 16 ? `${text.slice(0, 12)}…${text.slice(-4)}` : text;
}

function formatTokenCount(value: unknown): string {
  const count = Number(value ?? 0);
  return Number.isFinite(count) ? count.toLocaleString() : '—';
}

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value ?? '');
  }
}

export default function ProteinRunsWorkspace(props: ProteinRunsWorkspaceProps) {
  const [runs, setRuns] = createSignal<ProteinRunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = createSignal('');
  const [detail, setDetail] = createSignal<ProteinRunDetail | null>(null);
  const [loadingList, setLoadingList] = createSignal(true);
  const [loadingDetail, setLoadingDetail] = createSignal(false);
  const [error, setError] = createSignal('');
  const [actionError, setActionError] = createSignal('');
  const [actionNotice, setActionNotice] = createSignal('');
  const [deleteNotice, setDeleteNotice] = createSignal('');
  const [actionLoading, setActionLoading] = createSignal<'retry' | 'cancel' | 'reconcile' | 'delete' | ''>('');
  const [watcher, setWatcher] = createSignal<ProteinRunWatcherHealth | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = createSignal('');
  let listRequest = 0;
  let detailRequest = 0;
  let appliedFocusKey: number | undefined;

  const workspaceLabel = createMemo(() => props.workspaceId || 'workspace-1');

  const loadDetail = async (runId: string, showLoading = true) => {
    const normalized = String(runId || '').trim();
    if (!normalized) {
      setDetail(null);
      return;
    }
    const requestId = ++detailRequest;
    if (showLoading) setLoadingDetail(true);
    try {
      const payload = await getProteinRun(normalized, props.workspaceId);
      if (requestId !== detailRequest || selectedRunId() !== normalized) return;
      setDetail(payload);
      setRuns(items => items.some(item => item.run_id === payload.run.run_id) ? items : [...items, payload.run]);
      setWatcher(payload.watcher || null);
    } catch (reason) {
      if (requestId !== detailRequest) return;
      setError(reason instanceof Error ? reason.message : '读取 Run 详情失败');
      setDetail(null);
    } finally {
      if (requestId === detailRequest) setLoadingDetail(false);
    }
  };

  const loadRuns = async (showLoading = false) => {
    const requestId = ++listRequest;
    if (showLoading) setLoadingList(true);
    setError('');
    try {
      const payload = await listProteinRuns(props.workspaceId);
      if (requestId !== listRequest) return;
      const nextRuns = Array.isArray(payload.runs) ? payload.runs : [];
      setRuns(nextRuns);
      setWatcher(payload.watcher || null);
      const current = selectedRunId();
      const requested = props.focusRequest?.key !== appliedFocusKey ? props.focusRequest?.runId : '';
      const nextSelected = requested || (nextRuns.some((item) => item.run_id === current)
        ? current
        : current && detail()?.run.run_id === current ? current : nextRuns[0]?.run_id || '');
      setSelectedRunId(nextSelected);
      if (requested) appliedFocusKey = props.focusRequest?.key;
      setLastUpdatedAt(payload.generated_at || new Date().toISOString());
      if (nextSelected) {
        await loadDetail(nextSelected, showLoading);
      } else {
        setDetail(null);
      }
    } catch (reason) {
      if (requestId !== listRequest) return;
      setError(reason instanceof Error ? reason.message : '读取 Protein Run 列表失败');
    } finally {
      if (requestId === listRequest) setLoadingList(false);
    }
  };

  const selectRun = (runId: string) => {
    if (runId === selectedRunId() && detail()) return;
    setSelectedRunId(runId);
    setDetail(null);
    setError('');
    setActionError('');
    setActionNotice('');
    setDeleteNotice('');
    void loadDetail(runId, true);
  };

  createEffect(() => {
    const active = props.active !== false;
    const workspaceId = props.workspaceId || '';
    void props.focusRequest;
    if (!active) return;
    void workspaceId;
    setSelectedRunId('');
    setDetail(null);
    void loadRuns(true);
  });

  onMount(() => {
    const timer = window.setInterval(() => {
      if (props.active !== false && document.visibilityState !== 'hidden') void loadRuns(false);
    }, 8000);
    onCleanup(() => window.clearInterval(timer));
  });

  const current = createMemo(() => detail());
  const activeAttempt = createMemo<ProteinRunAttempt | null>(() => {
    const value = current()?.active_attempt;
    return value && typeof value === 'object' && String((value as ProteinRunAttempt).attempt_id || '').trim()
      ? value as ProteinRunAttempt
      : null;
  });

  const retryAttempt = async () => {
    const run = current()?.run;
    const attempt = activeAttempt();
    if (!run || !attempt) return;
    setActionLoading('retry');
    setActionError('');
    setActionNotice('');
    try {
      const result = await retryProteinRunAttempt(
        run.run_id,
        attempt.attempt_id,
        props.workspaceId,
      );
      setActionNotice(`已提交新的计算 attempt${result.attempt?.job_id ? ` · ${result.attempt.job_id}` : ''}`);
      await loadRuns(false);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '重试计算失败');
    } finally {
      setActionLoading('');
    }
  };

  const cancelAttempt = async () => {
    const run = current()?.run;
    const attempt = activeAttempt();
    if (!run || !attempt) return;
    setActionLoading('cancel');
    setActionError('');
    setActionNotice('');
    try {
      await cancelProteinRunAttempt(
        run.run_id,
        attempt.attempt_id,
        props.workspaceId,
      );
      setActionNotice('取消请求已发送，等待执行端确认。');
      await loadRuns(false);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '取消计算失败');
    } finally {
      setActionLoading('');
    }
  };

  const reconcileAttempt = async () => {
    const run = current()?.run;
    const attempt = activeAttempt();
    if (!run || !attempt) return;
    setActionLoading('reconcile');
    setActionError('');
    setActionNotice('');
    try {
      const result = await reconcileProteinRunAttempt(
        run.run_id,
        attempt.attempt_id,
        props.workspaceId,
      );
      if (result.status === 'succeeded') {
        setActionNotice('后台结果已重新同步到 Run 和原会话。');
      } else if (result.status === 'not_terminal') {
        setActionNotice('后台任务尚未结束，已恢复状态跟踪。');
      } else {
        setActionError(result.error || `重新同步未完成：${result.status}`);
      }
      await loadRuns(false);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '重新同步结果失败');
    } finally {
      setActionLoading('');
    }
  };

  const removeRun = async () => {
    const run = current()?.run;
    if (!run) return;
    if (run.deletable === false) {
      setActionError(run.delete_block_reason || '当前 Run 暂不允许删除。');
      return;
    }
    const confirmed = await confirmAction(
      `确认永久删除运行“${userFacingTitle(run.title, '蛋白计算')}”吗？该运行的输入快照、输出产物和检查点会一并删除，原聊天消息不受影响。`,
    );
    if (!confirmed) return;
    setActionLoading('delete');
    setActionError('');
    setActionNotice('');
    setDeleteNotice('');
    try {
      const result = await deleteProteinRun(run.run_id, props.workspaceId);
      detailRequest += 1;
      setRuns((items) => items.filter((item) => item.run_id !== run.run_id));
      setSelectedRunId('');
      setDetail(null);
      setDeleteNotice(
        `已删除运行（${result.removed_output_count} 个输出，${result.removed_attempt_count} 次计算尝试）`,
      );
      await loadRuns(false);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '删除 Protein Run 失败');
    } finally {
      setActionLoading('');
    }
  };

  return (
    <section class="flex min-h-0 min-w-0 flex-1 flex-col bg-[#0f1418]/76">
      <header class="flex flex-wrap items-start justify-between gap-3 border-b border-[#2a3136] bg-[#0d1216]/88 px-5 py-4">
        <div>
          <div class="text-[10px] font-semibold uppercase tracking-[0.24em] text-[#7bd0ff]">Protein Run Observatory</div>
          <h1 class="mt-1 text-xl font-semibold text-[#f7fafc]">蛋白任务运行记录</h1>
          <p class="mt-1 text-sm text-[#8b949e]">
            查看当前 Workspace 的输入、输出、工具步骤、安全审查和检查点；可受控取消或重试外部计算，不执行晋级操作。
          </p>
        </div>
        <div class="flex items-center gap-2">
          <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-1 text-[11px] text-[#9faeb7]">
            {workspaceLabel()}
          </span>
          <button
            type="button"
            class="inline-flex h-8 items-center justify-center rounded-full border border-[#303539] bg-[#171c20] px-3 text-xs font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/40 hover:text-[#8ed5ff] disabled:opacity-50"
            disabled={loadingList()}
            onClick={() => void loadRuns(true)}
          >
            {loadingList() ? '刷新中…' : '刷新'}
          </button>
        </div>
      </header>

      <Show when={error()}>
        <div class="mx-4 mt-4 rounded-xl border border-[#ef4444]/35 bg-[#ef4444]/10 px-4 py-3 text-sm text-[#fca5a5]">
          {error()}
        </div>
      </Show>

      <Show when={deleteNotice()}>
        <div class="mx-4 mt-4 rounded-xl border border-[#38b46a]/30 bg-[#38b46a]/8 px-4 py-3 text-sm text-[#9ee6bb]">
          {deleteNotice()}
        </div>
      </Show>

      <Show when={watcher()?.enabled && (!watcher()?.running || watcher()?.last_error || Number(watcher()?.sync_failed_count || 0) > 0)}>
        <div class="mx-4 mt-4 rounded-xl border border-[#f1a02b]/35 bg-[#f1a02b]/10 px-4 py-3 text-xs leading-5 text-[#ffc176]">
          <div class="font-semibold">
            {!watcher()?.running ? '异步监听器当前未运行' : Number(watcher()?.sync_failed_count || 0) > 0 ? `有 ${watcher()?.sync_failed_count} 个结果同步失败` : '异步监听器最近出现异常'}
          </div>
          <div class="mt-1 text-[#d7b477]">
            最后轮询：{formatTime(watcher()?.last_poll_at)}
            <Show when={watcher()?.last_error}> · {watcher()?.last_error}</Show>
          </div>
        </div>
      </Show>

      <div class="flex min-h-0 flex-1 flex-col overflow-hidden xl:flex-row">
        <aside class="flex max-h-[40%] w-full shrink-0 flex-col border-b border-[#2a3136] bg-[#11171b]/80 xl:max-h-none xl:w-[360px] xl:border-b-0 xl:border-r">
          <div class="flex items-center justify-between border-b border-[#242b30] px-4 py-3 text-xs text-[#8b949e]">
            <span>Run 列表 · {runs().length}</span>
            <span>{lastUpdatedAt() ? formatTime(lastUpdatedAt()) : '—'}</span>
          </div>
          <div class="min-h-0 flex-1 overflow-y-auto p-3">
            <Show
              when={!loadingList() || runs().length > 0}
              fallback={<div class="px-3 py-8 text-center text-sm text-[#71808a]">正在读取运行记录…</div>}
            >
              <Show
                when={runs().length > 0}
                fallback={
                  <div class="rounded-xl border border-dashed border-[#303539] bg-[#151b1f] px-4 py-8 text-center">
                    <div class="text-sm font-semibold text-[#bdc8d1]">当前 Workspace 暂无 Protein Run</div>
                    <div class="mt-2 text-xs leading-5 text-[#71808a]">正式的蛋白计算任务产生 Run 后，会自动显示在这里。</div>
                  </div>
                }
              >
                <div class="space-y-2">
                  <For each={runs()}>
                    {(run) => (
                      <button
                        type="button"
                        class={`w-full rounded-xl border p-3 text-left transition-colors ${selectedRunId() === run.run_id ? 'border-[#38bdf8]/45 bg-[#38bdf8]/10' : 'border-[#2a3136] bg-[#151b1f] hover:border-[#3e484f] hover:bg-[#171e23]'}`}
                        onClick={() => selectRun(run.run_id)}
                      >
                        <div class="flex items-start justify-between gap-2">
                          <div class="line-clamp-2 text-sm font-semibold leading-5 text-[#e6edf3]">{userFacingTitle(run.title, '蛋白计算')}</div>
                          <span class={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${statusClass(run.sync_status === 'sync_failed' ? 'sync_failed' : run.stale ? 'retry_wait' : run.lifecycle_status)}`}>
                            {run.sync_status === 'sync_failed' ? '结果同步失败' : run.stale ? '可能失联' : statusLabel(run.lifecycle_status)}
                          </span>
                        </div>
                        <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-[#8b949e]">
                          <span>工具 {run.tool_call_count}</span>
                          <span>输出 {run.output_count}</span>
                          <span>{formatTime(run.updated_at || run.created_at)}</span>
                        </div>
                      </button>
                    )}
                  </For>
                </div>
              </Show>
            </Show>
          </div>
        </aside>

        <main class="min-h-0 min-w-0 flex-1 overflow-y-auto p-4 md:p-5">
          <Show when={loadingDetail() && !current()}>
            <div class="flex min-h-[240px] items-center justify-center text-sm text-[#71808a]">正在读取 Run 详情…</div>
          </Show>
          <Show
            when={current()}
            fallback={
              <Show when={!loadingDetail() && runs().length > 0}>
                <div class="flex min-h-[240px] items-center justify-center text-sm text-[#71808a]">请选择一条 Run 查看详情。</div>
              </Show>
            }
          >
            <div class="mx-auto max-w-6xl space-y-4">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0">
                  <h2 class="mt-1 text-xl font-semibold text-[#f7fafc]">{userFacingTitle(current()!.run.title, '蛋白计算')}</h2>
                  <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-[#8b949e]">
                    <span>Agent · {current()!.run.agent_name || '—'}</span>
                    <Show when={current()!.run.worker_name}><span>Worker · {current()!.run.worker_name}</span></Show>
                  </div>
                </div>
                <div class="flex flex-wrap items-center justify-end gap-2">
                  <span class={`rounded-full border px-3 py-1 text-xs font-semibold ${statusClass(current()!.run.lifecycle_status)}`}>
                    {statusLabel(current()!.run.lifecycle_status)}
                  </span>
                  <button
                    type="button"
                    class="rounded-full border border-[#ef4444]/30 bg-[#ef4444]/10 px-3 py-1.5 text-xs font-semibold text-[#fca5a5] transition-colors hover:border-[#ef4444]/55 hover:bg-[#ef4444]/15 disabled:cursor-not-allowed disabled:opacity-45"
                    disabled={Boolean(actionLoading()) || current()!.run.deletable === false}
                    title={current()!.run.delete_block_reason || '永久删除此 Run 及其产物'}
                    onClick={() => void removeRun()}
                  >
                    {actionLoading() === 'delete' ? '删除中…' : '删除记录'}
                  </button>
                </div>
              </div>

              <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <div class={panelClass}>
                  <div class={mutedLabelClass}>输入</div>
                  <div class="mt-2 text-2xl font-semibold text-[#f7fafc]">{current()!.inputs.length}</div>
                </div>
                <div class={panelClass}>
                  <div class={mutedLabelClass}>输出</div>
                  <div class="mt-2 text-2xl font-semibold text-[#f7fafc]">{current()!.outputs.length}</div>
                </div>
                <div class={panelClass}>
                  <div class={mutedLabelClass}>工具 / MCP 步骤</div>
                  <div class="mt-2 text-2xl font-semibold text-[#f7fafc]">{current()!.tool_calls.length}</div>
                </div>
                <div class={panelClass}>
                  <div class={mutedLabelClass}>恢复次数</div>
                  <div class="mt-2 text-2xl font-semibold text-[#f7fafc]">{current()!.recovery.resume_count}</div>
                </div>
              </div>

              <div class="grid gap-4 lg:grid-cols-2">
                <section class={panelClass}>
                  <div class="flex items-start justify-between gap-3">
                    <div>
                      <div class={mutedLabelClass}>恢复状态</div>
                      <div class="mt-2 text-sm font-semibold text-[#e6edf3]">{recoveryLabel(current()!.recovery.state)}</div>
                    </div>
                    <span class="rounded-full border border-[#3e484f] bg-[#0f1418] px-2 py-1 text-[10px] text-[#9faeb7]">检查点只读</span>
                  </div>
                  <div class="mt-3 grid grid-cols-2 gap-3 text-xs text-[#9faeb7]">
                    <div>
                      <div class="text-[#71808a]">Checkpoint</div>
                      <div class="mt-1 text-[#d7dee3]">{statusLabel(current()!.recovery.checkpoint_status)}</div>
                    </div>
                    <div>
                      <div class="text-[#71808a]">已完成步骤</div>
                      <div class="mt-1 text-[#d7dee3]">{current()!.recovery.completed_step_count}</div>
                    </div>
                    <div>
                      <div class="text-[#71808a]">最近校验</div>
                      <div class="mt-1 text-[#d7dee3]">{formatTime(current()!.recovery.last_input_validation.checked_at)}</div>
                    </div>
                    <div>
                      <div class="text-[#71808a]">输入状态</div>
                      <div class="mt-1 text-[#d7dee3]">
                        {current()!.recovery.last_input_validation.valid === true
                          ? '上次校验通过'
                          : current()!.recovery.last_input_validation.valid === false
                            ? '校验失败'
                            : '尚未校验'}
                      </div>
                    </div>
                  </div>
                  <Show when={current()!.recovery.pending_action}>
                    <div class="mt-3 rounded-xl border border-[#303539] bg-[#0f1418] px-3 py-2 text-xs leading-5 text-[#bdc8d1]">
                      下一步：{current()!.recovery.pending_action}
                    </div>
                  </Show>
                  <Show when={current()!.recovery.requires_revalidation}>
                    <div class="mt-2 text-[11px] leading-5 text-[#ffc176]">真正恢复前仍需重新校验输入，本页不会直接触发执行。</div>
                  </Show>
                  <Show when={activeAttempt()}>
                    <div class="mt-3 flex flex-wrap gap-2 border-t border-[#2a3136] pt-3">
                      <JobActionControls
                        loading={actionLoading() === 'retry' ? 'retry' : actionLoading() === 'cancel' ? 'cancel' : ''}
                        retryAvailable={Boolean(current()!.action_policy.retry?.available)}
                        retryReason={String(current()!.action_policy.retry?.reason || '')}
                        cancelAvailable={Boolean(current()!.action_policy.cancel?.available)}
                        cancelReason={String(current()!.action_policy.cancel?.reason || '')}
                        showResubmit={false}
                        showResume={false}
                        showReasons
                        confirmRetry
                        confirmCancel
                        onRetry={() => void retryAttempt()}
                        onCancel={() => void cancelAttempt()}
                      />
                    </div>
                  </Show>
                  <Show when={actionNotice()}>
                    <div class="mt-3 rounded-xl border border-[#38b46a]/30 bg-[#38b46a]/8 px-3 py-2 text-xs text-[#9ee6bb]">{actionNotice()}</div>
                  </Show>
                  <Show when={actionError()}>
                    <div class="mt-3 rounded-xl border border-[#ef4444]/30 bg-[#ef4444]/8 px-3 py-2 text-xs text-[#fca5a5]">{actionError()}</div>
                  </Show>
                </section>

                <section class={panelClass}>
                  <div class={mutedLabelClass}>运行信息</div>
                  <dl class="mt-3 grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
                    <dt class="text-[#71808a]">创建时间</dt><dd class="text-[#d7dee3]">{formatTime(current()!.run.created_at)}</dd>
                    <dt class="text-[#71808a]">更新时间</dt><dd class="text-[#d7dee3]">{formatTime(current()!.run.updated_at)}</dd>
                    <dt class="text-[#71808a]">结束时间</dt><dd class="text-[#d7dee3]">{formatTime(current()!.run.finalized_at)}</dd>
                    <dt class="text-[#71808a]">任务标识</dt><dd class="truncate font-mono text-[#d7dee3]">{current()!.run.task_id || '—'}</dd>
                    <dt class="text-[#71808a]">交付物</dt><dd class="text-[#d7dee3]">{String(current()!.contract.deliverable || '—')}</dd>
                  </dl>
                  <Show when={current()!.run.error}>
                    <div class="mt-3 rounded-xl border border-[#ef4444]/30 bg-[#ef4444]/8 px-3 py-2 text-xs text-[#fca5a5]">{current()!.run.error}</div>
                  </Show>
                </section>
              </div>

              <section class={panelClass}>
                <div class="flex items-center justify-between gap-3">
                  <div>
                    <div class={mutedLabelClass}>后台计算</div>
                    <h3 class="mt-1 text-sm font-semibold text-[#e6edf3]">Attempt 历史</h3>
                  </div>
                  <div class="text-[11px] text-[#71808a]">失败 attempt 会保留；重试结果不会覆盖旧产物</div>
                </div>
                <Show when={current()!.attempts.length > 0} fallback={<div class="mt-4 text-xs text-[#71808a]">当前 Run 没有关联的后台计算 attempt。</div>}>
                  <div class="mt-4 space-y-2">
                    <For each={[...current()!.attempts].reverse()}>
                      {(attempt, index) => (
                        <div class={`rounded-xl border px-3 py-3 ${attempt.attempt_id === activeAttempt()?.attempt_id ? 'border-[#38bdf8]/35 bg-[#38bdf8]/7' : 'border-[#2a3136] bg-[#0f1418]'}`}>
                          <div class="flex flex-wrap items-start justify-between gap-2">
                            <div class="min-w-0">
                              <div class="text-xs font-semibold text-[#d7dee3]">Attempt {current()!.attempts.length - index()}</div>
                            </div>
                            <span class={`rounded-full border px-2 py-0.5 text-[10px] ${statusClass(attempt.status)}`}>{statusLabel(attempt.status)}</span>
                          </div>
                          <div class="mt-2 flex flex-wrap gap-3 text-[10px] text-[#8b949e]">
                            <span>{attempt.target_server || 'MCP'} · {attempt.target_tool || 'unknown tool'}</span>
                            <span>{formatTime(attempt.updated_at || attempt.submitted_at)}</span>
                          <Show when={attempt.superseded_by}><span>已由后续 attempt 替代</span></Show>
                            <Show when={attempt.stale}><span class="text-[#ffc176]">可能失联 · {Math.floor(Number(attempt.stale_seconds || 0) / 60)} 分钟未更新</span></Show>
                            <Show when={attempt.sync_status === 'retry_wait'}><span class="text-[#ffc176]">等待自动重新同步</span></Show>
                            <Show when={attempt.sync_status === 'sync_failed'}><span class="text-[#fca5a5]">结果同步失败</span></Show>
                          </div>
                          <Show when={attempt.result_summary}>
                            <div class="mt-2 whitespace-pre-line text-xs leading-5 text-[#bdc8d1]">
                              {formatAsyncResultSummary(attempt.result_summary).text}
                            </div>
                            <Show when={formatAsyncResultSummary(attempt.result_summary).structured}>
                              <details class="mt-2 rounded-xl border border-[#2a3136] bg-[#0f1418] px-3 py-2 text-[10px] text-[#71808a]">
                                <summary class="cursor-pointer select-none font-semibold">查看原始结果</summary>
                                <pre class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono leading-4 text-[#8b949e]">{formatAsyncResultSummary(attempt.result_summary).raw}</pre>
                              </details>
                            </Show>
                          </Show>
                          <Show when={attempt.error_summary}><div class="mt-2 text-xs text-[#fca5a5]">{attempt.error_summary}</div></Show>
                          <Show when={attempt.attempt_id === activeAttempt()?.attempt_id && (attempt.stale || attempt.sync_status === 'sync_failed')}>
                            <button
                              type="button"
                              class="mt-3 inline-flex h-8 items-center justify-center rounded-lg border border-[#f1a02b]/40 bg-[#f1a02b]/10 px-3 text-xs font-semibold text-[#ffc176] transition-colors hover:bg-[#f1a02b]/15 disabled:opacity-50"
                              disabled={Boolean(actionLoading())}
                              onClick={() => void reconcileAttempt()}
                            >
                              {actionLoading() === 'reconcile' ? '同步中…' : '重新同步结果'}
                            </button>
                          </Show>
                        </div>
                      )}
                    </For>
                  </div>
                </Show>
              </section>

              <section class={panelClass}>
                <div class="flex items-center justify-between gap-3">
                  <div>
                    <div class={mutedLabelClass}>输入与输出</div>
                    <h3 class="mt-1 text-sm font-semibold text-[#e6edf3]">科研文件</h3>
                  </div>
                  <div class="text-[11px] text-[#71808a]">输入仅展示沙箱路径；输出按 Manifest 白名单访问</div>
                </div>
                <div class="mt-4 grid gap-4 lg:grid-cols-2">
                  <div>
                    <div class="mb-2 text-xs font-semibold text-[#9faeb7]">输入文件</div>
                    <Show when={current()!.inputs.length > 0} fallback={<div class="text-xs text-[#71808a]">没有登记输入文件。</div>}>
                      <div class="space-y-2">
                        <For each={current()!.inputs}>
                          {(item) => (
                            <div class="rounded-xl border border-[#2a3136] bg-[#0f1418] px-3 py-2">
                              <div class="truncate text-xs font-semibold text-[#d7dee3]">{item.name || item.relative_path || '未命名输入'}</div>
                              <div class="mt-1 truncate font-mono text-[10px] text-[#71808a]">{item.container_path || item.relative_path || '—'}</div>
                              <div class="mt-2 flex flex-wrap gap-2 text-[10px] text-[#8b949e]">
                                <span>{item.mode || item.import_mode || 'input'}</span>
                                <span>{formatBytes(item.size)}</span>
                                <span>SHA {compactHash(item.sha256)}</span>
                                <Show when={item.read_only}><span class="text-[#8ed5ff]">只读</span></Show>
                              </div>
                            </div>
                          )}
                        </For>
                      </div>
                    </Show>
                  </div>
                  <div>
                    <div class="mb-2 text-xs font-semibold text-[#9faeb7]">输出文件</div>
                    <Show when={current()!.outputs.length > 0} fallback={<div class="text-xs text-[#71808a]">尚未生成输出文件。</div>}>
                      <div class="space-y-2">
                        <For each={current()!.outputs}>
                          {(item) => (
                            <a
                              class="block rounded-xl border border-[#2a3136] bg-[#0f1418] px-3 py-2 transition-colors hover:border-[#7bd0ff]/40"
                              href={proteinRunArtifactUrl(item.artifact_url || '', props.workspaceId)}
                              target="_blank"
                              rel="noreferrer"
                            >
                              <div class="truncate text-xs font-semibold text-[#8ed5ff]">{item.relative_path}</div>
                              <div class="mt-2 flex flex-wrap gap-2 text-[10px] text-[#8b949e]">
                                <span>{item.kind || 'file'}</span>
                                <span>{formatBytes(item.size)}</span>
                                <span>SHA {compactHash(item.sha256)}</span>
                              </div>
                            </a>
                          )}
                        </For>
                      </div>
                    </Show>
                  </div>
                </div>
              </section>

              <section class={panelClass}>
                <div class={mutedLabelClass}>工具调用</div>
                <h3 class="mt-1 text-sm font-semibold text-[#e6edf3]">MCP 与内置工具步骤</h3>
                <Show when={current()!.tool_calls.length > 0} fallback={<div class="mt-4 text-xs text-[#71808a]">当前 Run 没有登记工具调用。</div>}>
                  <div class="mt-4 space-y-2">
                    <For each={current()!.tool_calls}>
                      {(call, index) => (
                        <div class="rounded-xl border border-[#2a3136] bg-[#0f1418] px-3 py-3">
                          <div class="flex flex-wrap items-center justify-between gap-2">
                            <div class="flex min-w-0 items-center gap-2">
                              <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-[#3e484f] bg-[#171c20] font-mono text-[10px] text-[#8b949e]">{index() + 1}</span>
                              <span class="truncate text-xs font-semibold text-[#d7dee3]">{toolDisplayLabel(call.tool_name)}</span>
                            </div>
                            <span class={`rounded-full border px-2 py-0.5 text-[10px] ${statusClass(call.state)}`}>{statusLabel(call.state)}</span>
                          </div>
                          <div class="mt-2 flex flex-wrap gap-3 text-[10px] text-[#71808a]">
                            <span>Call · {call.call_id || '—'}</span>
                            <span>Attempt · {call.attempt || 1}</span>
                          </div>
                          <Show when={call.error}>
                            <div class="mt-2 text-xs text-[#fca5a5]">{call.error}</div>
                          </Show>
                          <Show when={call.arguments && Object.keys(call.arguments).length > 0}>
                            <details class="mt-2">
                              <summary class="cursor-pointer text-[11px] text-[#8ed5ff]">查看参数</summary>
                              <pre class="mt-2 max-h-64 overflow-auto rounded-lg border border-[#242b30] bg-[#0b1013] p-3 text-[10px] leading-5 text-[#9faeb7]">{jsonText(call.arguments)}</pre>
                            </details>
                          </Show>
                        </div>
                      )}
                    </For>
                  </div>
                </Show>
              </section>

              <div class="grid gap-4 lg:grid-cols-2">
                <section class={panelClass}>
                  <div class={mutedLabelClass}>模型来源</div>
                  <Show when={current()!.model_attempts.length > 0} fallback={<div class="mt-3 text-xs text-[#71808a]">没有模型调用记录。</div>}>
                    <div class="mt-3 space-y-2">
                      <For each={current()!.model_attempts}>
                        {(attempt) => (
                          <div class="rounded-xl border border-[#2a3136] bg-[#0f1418] px-3 py-2 text-xs">
                            <div class="font-semibold text-[#d7dee3]">{attempt.agent_name || 'Agent'} · {attempt.model || '未知模型'}</div>
                            <div class="mt-1 flex flex-wrap gap-3 text-[10px] text-[#71808a]">
                              <span>{attempt.provider || 'unknown provider'}</span>
                              <span>Token {formatTokenCount(attempt.usage?.total_tokens)}</span>
                              <span>{formatTime(attempt.recorded_at)}</span>
                            </div>
                          </div>
                        )}
                      </For>
                    </div>
                  </Show>
                </section>

                <section class={panelClass}>
                  <div class={mutedLabelClass}>生物安全</div>
                  <Show
                    when={Object.keys(current()!.biosafety || {}).length > 0}
                    fallback={<div class="mt-3 text-xs text-[#71808a]">当前 Run 没有记录生物安全上下文。</div>}
                  >
                    <pre class="mt-3 max-h-64 overflow-auto rounded-xl border border-[#2a3136] bg-[#0f1418] p-3 text-[10px] leading-5 text-[#9faeb7]">{jsonText(current()!.biosafety)}</pre>
                  </Show>
                </section>
              </div>
            </div>
          </Show>
        </main>
      </div>
    </section>
  );
}
