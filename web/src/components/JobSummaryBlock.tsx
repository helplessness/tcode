import { For, Show } from 'solid-js';
import type { JobSummaryContentBlock, ResultScalar } from '../utils/contentBlocks';

const displayValue = (value: ResultScalar, unit = '') => {
  if (value === null || value === '') return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  return `${String(value)}${unit ? ` ${unit}` : ''}`;
};

const displayStatus = (status: string) => ({
  completed: '已完成',
  failed: '失败',
  timed_out: '已超时',
  terminated: '已终止',
  cancelled: '已取消',
}[status.toLowerCase()] || status);

export default function JobSummaryBlock(props: { block: JobSummaryContentBlock }) {
  return (
    <section
      data-testid="job-result-summary"
      class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0f1418]"
    >
      <div class="border-b border-[#2a3136] px-4 py-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h3 class="text-sm font-semibold text-[#e5f0f5]">{props.block.title}</h3>
          <Show when={props.block.status}>
            <span class="rounded-full bg-[#173142] px-2.5 py-1 text-[11px] font-semibold text-[#8ed5ff]">
              {displayStatus(props.block.status)}
            </span>
          </Show>
        </div>
        <Show when={props.block.description}>
          <p class="mt-1.5 text-xs leading-relaxed text-[#9faeb7]">{props.block.description}</p>
        </Show>
      </div>
      <Show when={props.block.metrics.length > 0}>
        <div class="grid grid-cols-2 gap-px bg-[#2a3136] sm:grid-cols-3 lg:grid-cols-4">
          <For each={props.block.metrics}>
            {(metric) => (
              <div class="min-w-0 bg-[#11181d] px-4 py-3">
                <div class="truncate text-[11px] text-[#83929b]" title={metric.label}>
                  {metric.label}
                </div>
                <div
                  class="mt-1 truncate text-base font-semibold text-[#dce7ed]"
                  title={displayValue(metric.value, metric.unit)}
                >
                  {displayValue(metric.value, metric.unit)}
                </div>
              </div>
            )}
          </For>
        </div>
      </Show>
    </section>
  );
}
