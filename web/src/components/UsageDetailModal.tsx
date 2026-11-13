import { For, Show, createMemo, createSignal, onCleanup, onMount } from 'solid-js';
import { getSessionUsageSummary, getUsageActivity, type UsageActivityDay, type SessionUsageNode, type SessionUsageSummary, type TokenUsage } from '../api/client';
import { formatKnownCost as formatCost } from '../utils/runBudget';

interface UsageDetailModalProps {
  onClose?: () => void;
}

const emptyUsage: TokenUsage = {
  prompt_tokens: 0,
  completion_tokens: 0,
  total_tokens: 0,
};

function formatNumber(value: number | undefined) {
  return Number(value || 0).toLocaleString();
}

function formatYi(value: number | undefined) {
  return `${(Number(value || 0) / 1e8).toFixed(4)} 亿`;
}

function formatActivityYi(value: number) {
  return `${(value / 1e8).toFixed(8).replace(/\.?0+$/, '') || '0'} 亿 Token`;
}

function localDate(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function activityCalendar(days: UsageActivityDay[]) {
  const amounts = new Map(days.map((day) => [day.date, day.total_tokens]));
  const end = new Date();
  end.setHours(12, 0, 0, 0);
  const start = new Date(end);
  start.setDate(start.getDate() - 364);
  start.setDate(start.getDate() - start.getDay());
  const cells: { date: string; value: number; month: string }[] = [];
  for (const cursor = new Date(start); cursor <= end; cursor.setDate(cursor.getDate() + 1)) {
    const date = localDate(cursor);
    cells.push({ date, value: amounts.get(date) || 0, month: cursor.getDate() <= 7 && cursor.getDay() === 0 ? `${cursor.getMonth() + 1}月` : '' });
  }
  return cells;
}

function ActivityChart(props: { days: UsageActivityDay[] }) {
  const [mode, setMode] = createSignal<'daily' | 'weekly' | 'cumulative'>('daily');
  const cells = createMemo(() => activityCalendar(props.days));
  const max = createMemo(() => Math.max(1, ...cells().map((cell) => cell.value)));
  const cellColor = (value: number) => {
    if (!value) return '#3b4046';
    const strength = Math.log1p(value) / Math.log1p(max());
    return strength > .8 ? '#3675ce' : strength > .6 ? '#6496dd' : strength > .4 ? '#92b5e7' : '#c2d5f0';
  };
  const weekly = createMemo(() => {
    const result: { label: string; value: number }[] = [];
    for (let i = 0; i < cells().length; i += 7) {
      const group = cells().slice(i, i + 7);
      result.push({ label: group[0]?.date || '', value: group.reduce((sum, cell) => sum + cell.value, 0) });
    }
    return result;
  });
  const bars = createMemo(() => mode() === 'weekly' ? weekly() : weekly().map((week, index, all) => ({
    ...week, value: all.slice(0, index + 1).reduce((sum, item) => sum + item.value, 0),
  })));
  const barMax = createMemo(() => Math.max(1, ...bars().map((bar) => bar.value)));
  const activeDays = createMemo(() => props.days.filter((day) => day.total_tokens > 0).length);
  const peak = createMemo(() => Math.max(0, ...props.days.map((day) => day.total_tokens)));
  return <section class="mt-5 rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div><div class="text-sm font-semibold text-[#f5f7fa]">Token 活动</div><div class="mt-1 text-xs text-[#8b949e]">最近一年 · 根据有时间戳的会话事件统计；无逐日记录的旧用量不计入图表</div></div>
      <div class="flex gap-1 rounded-lg border border-[#303b46] p-1">
        <For each={[['daily', '每日'], ['weekly', '每周'], ['cumulative', '累计']] as const}>{([key, label]) => <button type="button" class={`rounded-md px-3 py-1 text-xs ${mode() === key ? 'bg-[#365c91] text-white' : 'text-[#9ba8b5] hover:text-white'}`} onClick={() => setMode(key)}>{label}</button>}</For>
      </div>
    </div>
    <div class="mt-4 flex gap-6 text-xs text-[#9ba8b5]"><span>活动天数 <strong class="text-[#e1e9f2]">{activeDays()}</strong></span><span>单日峰值 <strong class="text-[#e1e9f2]">{formatYi(peak())}</strong></span></div>
    <div class="mt-4 overflow-x-auto pb-2">
      <Show when={mode() === 'daily'} fallback={<div class="flex h-40 min-w-[620px] items-end gap-[3px] border-b border-[#33404b] pb-1"><For each={bars()}>{(bar) => <div class="min-w-0 flex-1 rounded-t bg-[#638dd0] transition-all" style={{ height: `${Math.max(bar.value ? 3 : 0, bar.value / barMax() * 100)}%` }} title={`${bar.label} 起：${formatActivityYi(bar.value)}`} />}</For></div>}>
        <div class="grid min-w-[720px] grid-flow-col grid-rows-7 gap-[4px]" style={{ 'grid-auto-columns': 'minmax(8px, 1fr)' }}>
          <For each={cells()}>{(cell) => <div class="aspect-square rounded-[4px]" style={{ background: cellColor(cell.value) }} title={`${cell.date}：${formatActivityYi(cell.value)}`} role="img" aria-label={`${cell.date}，${formatActivityYi(cell.value)}`} />}</For>
        </div>
        <div class="mt-2 flex min-w-[720px] justify-between text-[10px] text-[#87939e]"><For each={Array.from({ length: 12 }, (_, i) => i)}>{(i) => <span>{new Date(new Date().getFullYear(), new Date().getMonth() - 11 + i, 1).getMonth() + 1}月</span>}</For></div>
      </Show>
    </div>
  </section>;
}

function formatTitle(node: SessionUsageNode) {
  const title = String(node.title || '').trim();
  if (title) return title;
  return `${node.agent_name || 'session'} · ${node.session_id.slice(0, 8)}`;
}

function usagePercent(part: TokenUsage, total: TokenUsage) {
  const totalTokens = Number(total.total_tokens || 0);
  if (totalTokens <= 0) return '0%';
  return `${Math.round((Number(part.total_tokens || 0) / totalTokens) * 100)}%`;
}

function agentRows(byAgent: Record<string, TokenUsage>) {
  return Object.entries(byAgent || {})
    .map(([agent, usage]) => ({ agent, usage }))
    .sort((a, b) => Number(b.usage.total_tokens || 0) - Number(a.usage.total_tokens || 0));
}

function UsageLine(props: { label: string; usage: TokenUsage; total?: TokenUsage; cost?: number }) {
  return (
    <div class="grid grid-cols-[minmax(0,1.2fr)_0.7fr_0.7fr_0.7fr_0.5fr] items-center gap-3 border-t border-[#263039] px-3 py-2 text-xs first:border-t-0">
      <div class="min-w-0 truncate font-semibold text-[#dee3e8]" title={props.label}>{props.label === 'verification_models' ? '独立验证调用' : props.label}</div>
      <div class="text-right font-mono text-[#bdc8d1]">{formatNumber(props.usage.prompt_tokens)}</div>
      <div class="text-right font-mono text-[#bdc8d1]">{formatNumber(props.usage.completion_tokens)}</div>
      <div class="text-right font-mono text-[#7ee787]">{formatNumber(props.usage.total_tokens)}</div>
      <div class="text-right font-mono text-[#8b949e]">{props.total ? usagePercent(props.usage, props.total) : formatCost(props.cost)}</div>
    </div>
  );
}

function SessionNodeRow(props: { node: SessionUsageNode; depth?: number }) {
  const depth = () => props.depth || 0;
  const childCount = () => props.node.children?.length || 0;
  return (
    <>
      <div class="border-t border-[#263039] px-3 py-3 first:border-t-0">
        <div class="flex items-start justify-between gap-3" style={{ 'padding-left': `${depth() * 18}px` }}>
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <span class="truncate text-sm font-semibold text-[#f5f7fa]" title={formatTitle(props.node)}>{formatTitle(props.node)}</span>
              <span class="rounded-full border border-[#3e484f] bg-[#171c20] px-2 py-0.5 text-[10px] font-semibold text-[#bdc8d1]">{props.node.agent_name || 'unknown'}</span>
              <Show when={childCount() > 0}>
                <span class="rounded-full border border-[#38bdf8]/30 bg-[#38bdf8]/10 px-2 py-0.5 text-[10px] font-semibold text-[#8ed5ff]">子会话 {childCount()}</span>
              </Show>
            </div>
            <div class="mt-1 font-mono text-[10px] text-[#6f7d87]">{props.node.session_id}</div>
          </div>
          <div class="grid shrink-0 grid-cols-3 gap-3 text-right text-xs">
            <div>
              <div class="text-[#6f7d87]">主 agent</div>
              <div class="font-mono text-[#dee3e8]">{formatNumber(props.node.own_usage.total_tokens)}</div>
            </div>
            <div>
              <div class="text-[#6f7d87]">含子 agent</div>
              <div class="font-mono text-[#7ee787]">{formatNumber(props.node.total_usage.total_tokens)}</div>
            </div>
            <div>
              <div class="text-[#6f7d87]" title="按统一令牌单价估算，不含远端计算费用">令牌参考费用</div>
              <div class="font-mono text-[#bdc8d1]">{formatCost(props.node.cost)}</div>
            </div>
          </div>
        </div>
        <Show when={agentRows(props.node.by_agent).length > 1}>
          <div class="mt-3 overflow-hidden rounded-xl border border-[#263039] bg-[#0f1418]/82">
            <div class="grid grid-cols-[minmax(0,1.2fr)_0.7fr_0.7fr_0.7fr_0.5fr] gap-3 px-3 py-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-[#6f7d87]">
              <div>Agent</div>
              <div class="text-right">输入</div>
              <div class="text-right">输出</div>
              <div class="text-right">总计</div>
              <div class="text-right">占比</div>
            </div>
            <For each={agentRows(props.node.by_agent)}>
              {(item) => <UsageLine label={item.agent} usage={item.usage} total={props.node.total_usage} />}
            </For>
          </div>
        </Show>
      </div>
      <For each={props.node.children || []}>
        {(child) => <SessionNodeRow node={child} depth={depth() + 1} />}
      </For>
    </>
  );
}

export default function UsageDetailModal(props: UsageDetailModalProps) {
  const [summary, setSummary] = createSignal<SessionUsageSummary | null>(null);
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [activity, setActivity] = createSignal<UsageActivityDay[]>([]);
  const [activityError, setActivityError] = createSignal('');
  const [today, setToday] = createSignal(localDate(new Date()));

  const loadActivity = async () => {
    try {
      setActivity(await getUsageActivity());
      setActivityError('');
    } catch (e) {
      setActivityError(e instanceof Error ? e.message : '每日活动读取失败');
    }
  };

  const loadUsage = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getSessionUsageSummary({ includeArchived: true });
      if (!data) throw new Error('无法读取 token 用量');
      setSummary(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载 token 用量失败');
    } finally {
      setLoading(false);
    }
    await loadActivity();
  };

  onMount(() => {
    void loadUsage();
    const timer = window.setInterval(() => {
      const date = localDate(new Date());
      if (date !== today()) {
        setToday(date);
        void loadActivity();
      }
    }, 60_000);
    onCleanup(() => window.clearInterval(timer));
  });

  const totalUsage = createMemo(() => summary()?.total_usage || emptyUsage);
  const topAgents = createMemo(() => agentRows(summary()?.by_agent || {}));
  const sessions = createMemo(() => summary()?.sessions || []);

  return (
    <div
      class="fixed inset-0 z-[130] flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm"
      onClick={() => props.onClose?.()}
    >
      <div
        class="flex h-[min(86vh,820px)] w-full max-w-6xl flex-col overflow-hidden rounded-[28px] border border-[#2a3136] bg-[#11171b] shadow-[0_30px_90px_rgba(0,0,0,0.5)]"
        onClick={(event) => event.stopPropagation()}
      >
        <div class="flex items-center justify-between border-b border-[#2a3136] px-6 py-5">
          <div>
            <div class="text-[11px] font-semibold uppercase tracking-[0.24em] text-[#7bd0ff]">Token 用量</div>
            <div class="mt-1 text-lg font-semibold text-[#f5f7fa]">会话与 Agent 消耗明细</div>
            <div class="mt-1 text-sm text-[#8b949e]">按 session 树聚合主 agent 与子 agent 的模型 token。</div>
          </div>
          <div class="flex items-center gap-2">
            <button
              class="inline-flex h-9 items-center justify-center rounded-xl border border-[#303539] bg-[#171c20] px-4 text-sm text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
              onClick={() => void loadUsage()}
              disabled={loading()}
            >
              {loading() ? '刷新中...' : '刷新'}
            </button>
            <button
              class="inline-flex h-9 items-center justify-center rounded-xl border border-[#303539] bg-[#171c20] px-4 text-sm text-[#8b949e] transition-colors hover:border-[#7bd0ff]/35 hover:text-[#8ed5ff]"
              onClick={() => props.onClose?.()}
            >
              关闭
            </button>
          </div>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto p-5">
          <Show when={error()}>
            <div class="mb-4 rounded-xl border border-[#ef4444]/35 bg-[#ef4444]/10 px-4 py-3 text-sm text-[#fca5a5]">{error()}</div>
          </Show>

          <div class="grid gap-3 md:grid-cols-4">
            <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
              <div class="text-xs text-[#7f8b93]">总 token</div>
              <div class="mt-2 font-mono text-2xl font-semibold text-[#7ee787]" title={formatNumber(totalUsage().total_tokens)}>{formatYi(totalUsage().total_tokens)}</div>
            </div>
            <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
              <div class="text-xs text-[#7f8b93]">输入 token</div>
              <div class="mt-2 font-mono text-2xl font-semibold text-[#dee3e8]" title={formatNumber(totalUsage().prompt_tokens)}>{formatYi(totalUsage().prompt_tokens)}</div>
            </div>
            <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
              <div class="text-xs text-[#7f8b93]">输出 token</div>
              <div class="mt-2 font-mono text-2xl font-semibold text-[#dee3e8]" title={formatNumber(totalUsage().completion_tokens)}>{formatYi(totalUsage().completion_tokens)}</div>
            </div>
            <div class="rounded-2xl border border-[#2a3136] bg-[#13191d] p-4">
              <div class="text-xs text-[#7f8b93]">令牌参考费用（统一单价估算，不含远端计算）</div>
              <div class="mt-2 font-mono text-2xl font-semibold text-[#ffc176]">{formatCost(summary()?.cost)}</div>
            </div>
          </div>

          <p class="mt-3 text-xs text-[#9ba8b5]">已计入新增验证调用的已知用量，主会话中的修复调用不会重复累加。验证缺失用量调用：{Number(summary()?.verification_accounting?.unknown_calls || 0)}；计量不可用会话：{Number(summary()?.verification_accounting?.unavailable_sessions || 0)}。未知用量及历史未记录费用不代表零消耗。</p>
          <Show when={activityError()}><div class="mt-4 text-xs text-[#fca5a5]">{activityError()}</div></Show>
          <ActivityChart days={activity()} />

          <div class="mt-5 grid gap-5 xl:grid-cols-[0.85fr_1.15fr]">
            <section class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#13191d]">
              <div class="border-b border-[#2a3136] px-4 py-3 text-sm font-semibold text-[#f5f7fa]">按 Agent 汇总</div>
              <div class="grid grid-cols-[minmax(0,1.2fr)_0.7fr_0.7fr_0.7fr_0.5fr] gap-3 px-3 py-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-[#6f7d87]">
                <div>Agent</div>
                <div class="text-right">输入</div>
                <div class="text-right">输出</div>
                <div class="text-right">总计</div>
                <div class="text-right">占比</div>
              </div>
              <Show when={topAgents().length > 0} fallback={<div class="px-4 py-8 text-center text-sm text-[#8b949e]">暂无 token 记录</div>}>
                <For each={topAgents()}>
                  {(item) => <UsageLine label={item.agent} usage={item.usage} total={totalUsage()} />}
                </For>
              </Show>
            </section>

            <section class="overflow-hidden rounded-2xl border border-[#2a3136] bg-[#13191d]">
              <div class="border-b border-[#2a3136] px-4 py-3 text-sm font-semibold text-[#f5f7fa]">按 Session 拆分</div>
              <Show when={sessions().length > 0} fallback={<div class="px-4 py-8 text-center text-sm text-[#8b949e]">暂无 session 用量</div>}>
                <For each={sessions()}>
                  {(node) => <SessionNodeRow node={node} />}
                </For>
              </Show>
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}
