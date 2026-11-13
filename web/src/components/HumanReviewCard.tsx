import { For, Show, createEffect, createSignal, onCleanup } from 'solid-js';
import { getHumanReviewQueue, type HumanReviewItem } from '../api/humanReview';
import type { ToolCall } from '../stores/session';
import { toolDisplayLabel } from '../utils/userFacingLabels';

export type ReviewReference = { reviewId: string; reviewIdentifier: string };
export function reviewReferences(calls: ToolCall[]): ReviewReference[] {
  const seen = new Set<string>();
  return calls.flatMap(call => {
    const data = call.metadata || {};
    if (!data.human_review_required) return [];
    const reviewId = String(data.id || data.issue_id || '');
    const reviewIdentifier = String(data.identifier || data.issue_identifier || '');
    const key = reviewId || reviewIdentifier;
    if (!key || seen.has(key)) return [];
    seen.add(key);
    return [{reviewId, reviewIdentifier}];
  });
}

export function reviewPhase(review: HumanReviewItem): string {
  if (review.status === 'pending_review') return '等待审核';
  if (review.status === 'rejected') return '已拒绝';
  if (['deleted', 'cancelled', 'canceled'].includes(review.status)) return '已取消';
  const nativeStatus = review.resume_result?.native_tool_status;
  if (nativeStatus === 'completed') return '已批准，操作已完成';
  if (nativeStatus === 'failed') return '已批准，但操作执行失败';
  if (['cancelled', 'canceled', 'terminated'].includes(nativeStatus)) return '已批准，操作已取消';
  if (review.resume_status === 'failed') return '已批准，但提交或恢复失败';
  const result = review.resume_result || {};
  if (result.started === true && ['queued', 'deduped'].includes(result.approved_action?.status)) return '已受理，计算结果待返回';
  if (review.status === 'approved') return '已批准，执行状态待确认';
  return '审核状态待确认';
}

export function reviewSummary(review: HumanReviewItem): Array<{label: string; value: string}> {
  const raw = review.tool_args || {};
  const args = raw.input_config && typeof raw.input_config === 'object' ? raw.input_config : raw;
  const labels: Record<string, string> = { model: '模型', sampling_steps: '采样步数', diffusion_samples: '结构样本数',
    devices: '设备数', recycling_steps: '循环次数', output_format: '输出格式', seed: '随机种子',
    use_potentials: '势能引导', affinity_ligand_index: '亲和力预测',
    max_sequences: '最多返回序列数', evalue: 'E-value 阈值', threads: '线程数', query_id: '目标序列名称' };
  const input = args.tools_input ?? args.pdb ?? args.fasta ?? args.alignment;
  const inputName = typeof input === 'string' ? input.split(/[\\/]/).pop()?.replace(/-[a-f0-9]{32}(?=\.)/i, '') : input ? '已授权的输入产物' : '';
  const rows = inputName ? [{label:'输入文件', value:inputName}] : [];
  const positions = args.positions;
  if (Array.isArray(positions) && positions.length > 0 && positions.length <= 500
      && positions.every(p => typeof p === 'string' && /^[ARNDCQEGHILKMFPSTWYV][A-Za-z0-9]-?[0-9]{1,10}[ARNDCQEGHILKMFPSTWYVa]$/.test(p))) {
    rows.push({label:'扫描位点（PDB编号）', value:positions.join(', ')});
  }
  return [...rows, ...Object.entries(labels).flatMap(([key, label]) => {
    const value = args[key];
    // Only display bounded scientific scalar parameters, never server/path/token fields.
    if (value === undefined || (typeof value === 'string' && !/^[\w.+-]{1,40}$/.test(value))) return [];
    if (value !== null && !['string', 'number', 'boolean'].includes(typeof value)) return [];
    return [{label, value: value === null ? '不启用' : typeof value === 'boolean' ? value ? '开启' : '关闭' : String(value)}];
  })];
}

export default function HumanReviewCard(props: { reference: ReviewReference; sessionId: string }) {
  const [review, setReview] = createSignal<HumanReviewItem>();
  const [error, setError] = createSignal('');
  const [loading, setLoading] = createSignal(false);
  let reload = () => {};
  createEffect(() => {
    const target = {...props.reference, sessionId: props.sessionId};
    let disposed = false, inflight = false;
    setReview(undefined); setError('');
    const load = async () => {
      if (disposed || inflight) return;
      inflight = true; setLoading(true);
      try {
        const result = await getHumanReviewQueue('', 1, target);
        if (!disposed) { setReview(result.reviews[0]); setError(result.reviews.length ? '' : '当前无法找到这份审核记录，请打开审核详情核对。'); }
      } catch { if (!disposed) setError('审核状态加载失败，请重试；未执行任何批准操作。'); }
      finally { inflight = false; if (!disposed) setLoading(false); }
    };
    reload = () => void load();
    void load();
    const timer = setInterval(() => { if (!document.hidden && (!review() || review()?.status === 'pending_review' || ['queued', 'running'].includes(review()?.resume_status || ''))) void load(); }, 5000);
    onCleanup(() => { disposed = true; clearInterval(timer); });
  });
  const open = () => window.dispatchEvent(new CustomEvent('tcode-reveal-human-review-panel', {detail: props.reference}));
  return <section class="tcode-review-notice my-2 rounded-lg border px-3 py-2" data-status={error() || review()?.resume_status === 'failed' ? 'failed' : review()?.status || 'loading'} aria-label="计算审核申请">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <strong class="min-w-0 text-xs">{review() ? toolDisplayLabel(review()!.tool_name) : '计算审核申请'}</strong>
      <span role="status" class="tcode-review-notice-status text-[11px]">{error() ? '状态未知' : review() ? reviewPhase(review()!) : '正在加载审核状态…'}</span>
      <button type="button" onClick={open} class="shrink-0 rounded-md border px-2 py-1 text-xs">{review()?.status === 'pending_review' ? '查看并审核' : '审核详情'}</button>
    </div>
    <Show when={review()}>{item => <>
      <details class="mt-1 text-xs"><summary class="cursor-pointer text-[var(--tcode-text-muted)]">参数与操作范围</summary>
      <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs"><For each={reviewSummary(item())}>{row => <span>{row.label}：{row.value}</span>}</For></div>
      <p class="mt-2 text-xs">{item().review_data?.goal_computation ? '仅批准所列参数的一次计算；不包含重跑或后续计算。资源成本与时长未确认。' : '请查看本次操作的输入和影响范围。'}</p>
      <Show when={item().resume_status === 'failed'}><p role="alert" class="mt-2 text-xs">执行未成功，不会自动重提；请查看审核详情中的恢复状态。</p></Show>
      </details>
    </>}</Show>
    <Show when={error()}><p role="alert" class="mt-2 text-xs">{error()} <button type="button" disabled={loading()} onClick={() => reload()} class="underline">重试</button></p></Show>
  </section>;
}
