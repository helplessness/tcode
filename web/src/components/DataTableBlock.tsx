import { For, Show, createMemo, createSignal } from 'solid-js';
import type { DataTableColumn, DataTableContentBlock, ResultScalar } from '../utils/contentBlocks';

const displayValue = (value: ResultScalar) => {
  if (value === null || value === '') return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
};

// 固定列宽：让每个单元格都有稳定、可预期的宽度，多列/长文本时统一左右滚动而不是互相挤压。
const columnWidthClass = (column: DataTableColumn) => {
  switch (column.type) {
    case 'boolean':
      return 'w-24'; // 96px
    case 'integer':
    case 'number':
      return 'w-32'; // 128px
    default:
      return 'w-48'; // 192px，字符串列给足文本空间
  }
};

export default function DataTableBlock(props: { block: DataTableContentBlock }) {
  const [sort, setSort] = createSignal<{ key: string; direction: 'asc' | 'desc' } | null>(
    props.block.default_sort || null
  );
  const sortedRows = createMemo(() => {
    const current = sort();
    if (!current) return props.block.rows;
    const direction = current.direction === 'asc' ? 1 : -1;
    return [...props.block.rows].sort((left, right) => {
      const a = left[current.key];
      const b = right[current.key];
      if (a === null && b === null) return 0;
      if (a === null) return 1;
      if (b === null) return -1;
      if (typeof a === 'number' && typeof b === 'number') return (a - b) * direction;
      return String(a).localeCompare(String(b), 'zh-CN', { numeric: true }) * direction;
    });
  });
  const changeSort = (key: string) => {
    setSort((current) => ({
      key,
      direction: current?.key === key && current.direction === 'asc' ? 'desc' : 'asc',
    }));
  };

  return (
    <section
      data-testid="job-result-table"
      class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#0f1418]"
    >
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#2a3136] px-4 py-3">
        <h3 class="text-sm font-semibold text-[#e5f0f5]">{props.block.title}</h3>
        <span class="text-[11px] text-[#83929b]">
          显示 {props.block.rows.length} / {props.block.total_rows} 条
        </span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full table-fixed border-collapse text-left text-xs">
          <thead class="bg-[#121a1f] text-[#9faeb7]">
            <tr>
              <For each={props.block.columns}>
                {(column) => {
                  const active = () => sort()?.key === column.key;
                  return (
                    <th class={`${columnWidthClass(column)} border-b border-[#2a3136] px-3 py-2.5 font-medium`}>
                      <button
                        type="button"
                        class="flex w-full items-center gap-1 text-left hover:text-[#dce7ed]"
                        onClick={() => changeSort(column.key)}
                        title={`${column.title}${column.unit ? ` (${column.unit})` : ''}（点击排序）`}
                      >
                        <span class="min-w-0 truncate">{column.title}{column.unit ? ` (${column.unit})` : ''}</span>
                        <span class={`shrink-0 ${active() ? 'text-[#8ed5ff]' : 'text-[#52616a]'}`}>
                          {active() ? (sort()?.direction === 'asc' ? '↑' : '↓') : '↕'}
                        </span>
                      </button>
                    </th>
                  );
                }}
              </For>
            </tr>
          </thead>
          <tbody class="text-[#c8d5dc]">
            <For each={sortedRows()}>
              {(row) => (
                <tr class="border-b border-[#222a2f] last:border-0 hover:bg-[#121a1f]">
                  <For each={props.block.columns}>
                    {(column) => (
                      <td class={`${columnWidthClass(column)} px-3 py-2.5`}>
                        <span class="block truncate" title={displayValue(row[column.key])}>
                          {displayValue(row[column.key])}
                        </span>
                      </td>
                    )}
                  </For>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </div>
      <Show when={props.block.rows.length === 0}>
        <div class="px-4 py-6 text-center text-xs text-[#83929b]">没有可显示的数据</div>
      </Show>
    </section>
  );
}
