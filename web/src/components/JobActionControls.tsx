import { confirmAction } from './FeedbackDialog';
import { Show } from 'solid-js';

export type JobActionName = 'retry' | 'resubmit' | 'resume' | 'cancel' | 'copy' | '';

type JobActionControlsProps = {
  loading?: JobActionName;
  retryAvailable?: boolean;
  retryReason?: string;
  resumeAvailable?: boolean;
  resumeReason?: string;
  cancelAvailable?: boolean;
  cancelReason?: string;
  showResubmit?: boolean;
  showResume?: boolean;
  showReasons?: boolean;
  confirmRetry?: boolean;
  confirmCancel?: boolean;
  onRetry?: () => void;
  onResubmit?: () => void;
  onResume?: () => void;
  onCancel?: () => void;
};

const neutralButtonClass = 'rounded-full border border-[#3e484f] bg-[#171c20] px-3 py-2 text-xs font-semibold text-[#bdc8d1] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff] disabled:opacity-50';
const retryButtonClass = 'rounded-full border border-[#d8ceff]/28 bg-[#7b61ff]/10 px-3 py-2 text-xs font-semibold text-[#d8ceff] disabled:opacity-50';
const resumeButtonClass = 'rounded-full border border-[#f1a02b]/28 bg-[#f1a02b]/12 px-3 py-2 text-xs font-semibold text-[#ffe1b3] disabled:opacity-50';
const cancelButtonClass = 'rounded-full border border-[#ef4444]/30 bg-[#ef4444]/10 px-3 py-2 text-xs font-semibold text-[#fca5a5] disabled:opacity-50';

export default function JobActionControls(props: JobActionControlsProps) {
  const invokeRetry = async () => {
    if (props.confirmRetry && !await confirmAction('确认使用完全相同的输入和参数重新提交计算吗？')) return;
    props.onRetry?.();
  };

  const invokeCancel = async () => {
    if (props.confirmCancel && !await confirmAction('确认请求取消当前后台计算吗？远程执行端可能需要一段时间才能确认。')) return;
    props.onCancel?.();
  };

  return (
    <>
      <Show when={props.onRetry}>
        <button
          type="button"
          class={retryButtonClass}
          onClick={invokeRetry}
          disabled={Boolean(props.loading) || props.retryAvailable === false}
        >
          {props.loading === 'retry' ? '重试中...' : '重试任务'}
        </button>
      </Show>
      <Show when={props.showResubmit !== false && props.onResubmit}>
        <button
          type="button"
          class={neutralButtonClass}
          onClick={() => props.onResubmit?.()}
          disabled={Boolean(props.loading) || props.retryAvailable === false}
        >
          {props.loading === 'resubmit' ? '提交中...' : '重新提交同参数'}
        </button>
      </Show>
      <Show when={props.showResume !== false && props.onResume}>
        <button
          type="button"
          class={resumeButtonClass}
          onClick={() => props.onResume?.()}
          disabled={Boolean(props.loading) || props.resumeAvailable === false}
        >
          {props.loading === 'resume' ? '恢复中...' : '从当前结果恢复'}
        </button>
      </Show>
      <Show when={props.onCancel}>
        <button
          type="button"
          class={cancelButtonClass}
          onClick={invokeCancel}
          disabled={Boolean(props.loading) || props.cancelAvailable === false}
        >
          {props.loading === 'cancel' ? '取消中...' : '取消计算'}
        </button>
      </Show>
      <Show when={props.showReasons && props.retryAvailable === false && props.retryReason}>
        <div class="w-full text-[10px] leading-5 text-[#d29922]">当前不可重试：{props.retryReason}</div>
      </Show>
      <Show when={props.showReasons && props.cancelAvailable === false && props.cancelReason}>
        <div class="w-full text-[10px] leading-5 text-[#8b949e]">当前不可取消：{props.cancelReason}</div>
      </Show>
      <Show when={props.showReasons && props.showResume !== false && props.resumeAvailable === false && props.resumeReason}>
        <div class="w-full text-[10px] leading-5 text-[#8b949e]">恢复不可用：{props.resumeReason}</div>
      </Show>
    </>
  );
}
