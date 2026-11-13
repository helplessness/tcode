import { Show } from 'solid-js';
import type { HarnessStateProjection } from '../api/harness';

const MEMORY_RISK = '系统仅继承最近 8 轮文本和一份简短交接摘要；Harness 私有记忆、完整研究状态及未完成工具链不会迁移。研究中途切换可能丢失记忆并显著降低任务效果。';

export default function HarnessSwitchNotice(props: {
  state: HarnessStateProjection;
  onDismiss: () => void;
}) {
  return (
    <div
      data-testid="harness-switch-notice"
      role="status"
      class="mb-2 flex items-start gap-3 rounded-xl border border-[#f1a02b]/45 bg-[#f1a02b]/10 px-3 py-2.5 text-xs text-[#ffe1b3]"
    >
      <span aria-hidden="true" class="mt-0.5 text-base">⚠</span>
      <div class="min-w-0 flex-1">
        <div class="font-semibold">
          已切换到 {props.state.runtime_id}
        </div>
        <div class="mt-0.5 leading-5 text-[#e8c99b]">{MEMORY_RISK}</div>
        <Show when={props.state.summary_status === 'fallback'}>
          <div class="mt-1 text-[#ffc176]">摘要模型不可用，已使用基础上下文完成切换。</div>
        </Show>
        <Show when={props.state.truncated}>
          <div class="mt-1 text-[#ffc176]">历史超过继承上限，较早原文已裁剪。</div>
        </Show>
      </div>
      <button
        type="button"
        aria-label="关闭 Harness 切换提示"
        class="rounded px-1.5 py-0.5 text-[#d4ad75] hover:bg-[#f1a02b]/15 hover:text-white"
        onClick={props.onDismiss}
      >
        ×
      </button>
    </div>
  );
}
