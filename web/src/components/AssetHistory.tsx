import { For, Show, createEffect, createSignal, on, onCleanup, onMount } from 'solid-js';

type Entry = { size: number; hash?: string };
type Change = { path: string; status: string; before?: Entry; after?: Entry };
type Run = { id: string; session: string; started: number; ended?: number };
type History = { selected_run?: Run; changes: Change[]; selected_change?: Change; change_total?: number; change_offset?: number; diff?: string; unavailable?: string; notice?: string; incomplete?: boolean; live?: boolean };
const RUN_PAGE = 50;
const CHANGE_PAGE = 100;
const statusName: Record<string, string> = { added: '新增', deleted: '删除', modified: '修改' };

function runName(item?: Run) {
  if (!item) return '选择任务记录';
  const source = item.id.startsWith('job:') ? `异步作业 ${item.id.slice(4)}` : `Agent 轮次 ${item.session.slice(0, 8)}`;
  return `${new Date(item.started * 1000).toLocaleString()} · ${source} · ${item.ended ? '已结束' : '未结束'}`;
}

export default function AssetHistory(props: { workspaceId: string; onPreview?: (path: string) => void; onQuote?: (text: string) => void }) {
  const [runs, setRuns] = createSignal<Run[]>([]);
  const [runTotal, setRunTotal] = createSignal(0);
  const [runOffset, setRunOffset] = createSignal(0);
  const [dateFrom, setDateFrom] = createSignal('');
  const [dateTo, setDateTo] = createSignal('');
  const [pickerOpen, setPickerOpen] = createSignal(false);
  const [data, setData] = createSignal<History>();
  const [run, setRun] = createSignal('');
  const [selectedRun, setSelectedRun] = createSignal<Run>();
  const [path, setPath] = createSignal('');
  const [changeQuery, setChangeQuery] = createSignal('');
  const [changeStatus, setChangeStatus] = createSignal('');
  const [changeOffset, setChangeOffset] = createSignal(0);
  const [error, setError] = createSignal('');
  const [loading, setLoading] = createSignal(false);
  let pickerRef: HTMLDivElement | undefined;
  let controller: AbortController | undefined;

  async function loadRuns(offset = 0, selectFirst = false, from = dateFrom(), to = dateTo()) {
    const query = new URLSearchParams({ workspace_id: props.workspaceId, offset: String(offset), limit: String(RUN_PAGE) });
    if (from) query.set('started_from', String(new Date(`${from}T00:00:00`).getTime() / 1000));
    if (to) {
      const exclusiveEnd = new Date(`${to}T00:00:00`);
      exclusiveEnd.setDate(exclusiveEnd.getDate() + 1);
      query.set('started_before', String(exclusiveEnd.getTime() / 1000));
    }
    const response = await fetch(`/api/files/history/runs?${query}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '任务记录加载失败');
    setRuns(result.runs || []); setRunTotal(result.total || 0); setRunOffset(result.offset || 0);
    if (selectFirst && result.runs?.length) await selectRun(result.runs[0]);
  }

  async function loadChanges(selectedRunId = run(), offset = changeOffset()) {
    if (!selectedRunId) { setData(undefined); return; }
    controller?.abort();
    const current = new AbortController(); controller = current;
    setLoading(true); setError('');
    try {
      const query = new URLSearchParams({ workspace_id: props.workspaceId, run_id: selectedRunId, change_q: changeQuery(), change_status: changeStatus(), change_offset: String(offset), change_limit: String(CHANGE_PAGE) });
      const response = await fetch(`/api/files/history?${query}`, { signal: current.signal });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '改动加载失败');
      if (!current.signal.aborted) { setData(result); setSelectedRun(result.selected_run); setChangeOffset(result.change_offset || 0); }
    } catch (e) { if (!current.signal.aborted) setError(e instanceof Error ? e.message : '加载失败'); }
    finally { if (!current.signal.aborted) setLoading(false); }
  }

  async function selectRun(item: Run) {
    setRun(item.id); setSelectedRun(item); setPath(''); setChangeOffset(0); setPickerOpen(false);
    await loadChanges(item.id, 0);
  }

  async function loadDiff(item: Change) {
    controller?.abort();
    const current = new AbortController(); controller = current;
    setPath(item.path); setLoading(true); setError('');
    try {
      const query = new URLSearchParams({ workspace_id: props.workspaceId, run_id: run(), path: item.path });
      const response = await fetch(`/api/files/history?${query}`, { signal: current.signal });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '差异加载失败');
      if (!current.signal.aborted) setData(previous => previous ? { ...previous, selected_change: result.selected_change, diff: result.diff, unavailable: result.unavailable } : result);
    } catch (e) { if (!current.signal.aborted) setError(e instanceof Error ? e.message : '加载失败'); }
    finally { if (!current.signal.aborted) setLoading(false); }
  }

  createEffect(on(() => props.workspaceId, () => {
    setRuns([]); setRun(''); setSelectedRun(undefined); setPath(''); setData(undefined); setDateFrom(''); setDateTo(''); setChangeQuery(''); setChangeStatus('');
    void loadRuns(0, true).catch(e => setError(e instanceof Error ? e.message : '加载失败'));
  }));
  onMount(() => {
    const closePicker = (event: PointerEvent) => {
      if (pickerOpen() && pickerRef && !pickerRef.contains(event.target as Node)) setPickerOpen(false);
    };
    document.addEventListener('pointerdown', closePicker);
    onCleanup(() => document.removeEventListener('pointerdown', closePicker));
  });
  onCleanup(() => controller?.abort());
  const selectedChange = () => data()?.selected_change || data()?.changes.find(item => item.path === path());
  const changeTotal = () => data()?.change_total || 0;

  return <section class="asset-history-panel min-h-0 flex-1 overflow-auto p-4" aria-label="资产版本差异">
    <div class="flex flex-wrap gap-2 items-center">
      <div ref={pickerRef} class="asset-history-picker relative min-w-0 flex-1 sm:max-w-2xl">
        <button class="asset-history-picker-trigger btn btn-sm w-full justify-between font-normal" data-selected={Boolean(run())} aria-haspopup="listbox" aria-expanded={pickerOpen()} onClick={() => setPickerOpen(!pickerOpen())}>
          <span class="truncate">{runName(selectedRun())}</span><span aria-hidden="true">⌄</span>
        </button>
        <Show when={pickerOpen()}>
          <div class="asset-history-picker-menu absolute mt-1 w-full rounded-box border p-2">
            <form class="space-y-2" onSubmit={e => { e.preventDefault(); void loadRuns(0).catch(error => setError(String(error))); }}>
              <div class="grid grid-cols-[1fr_auto_1fr] items-center gap-2">
                <label class="min-w-0 text-xs">开始日期<input type="date" class="input input-sm mt-1 w-full" value={dateFrom()} max={dateTo() || undefined} onInput={e => setDateFrom(e.currentTarget.value)} /></label>
                <span class="mt-5 opacity-50">至</span>
                <label class="min-w-0 text-xs">结束日期<input type="date" class="input input-sm mt-1 w-full" value={dateTo()} min={dateFrom() || undefined} onInput={e => setDateTo(e.currentTarget.value)} /></label>
              </div>
              <div class="flex flex-wrap gap-1">
                <For each={[{ label: '今天', days: 1 }, { label: '最近 7 天', days: 7 }, { label: '最近 30 天', days: 30 }]}>{preset =>
                  <button type="button" class="btn btn-xs" onClick={() => {
                    const end = new Date();
                    const start = new Date(); start.setDate(start.getDate() - preset.days + 1);
                    const localDate = (value: Date) => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
                    const from = localDate(start), to = localDate(end);
                    setDateFrom(from); setDateTo(to); void loadRuns(0, false, from, to);
                  }}>{preset.label}</button>
                }</For>
                <button type="button" class="btn btn-xs" onClick={() => { setDateFrom(''); setDateTo(''); void loadRuns(0, false, '', ''); }}>全部</button>
                <button class="btn btn-xs ml-auto" type="submit">按日期筛选</button>
              </div>
            </form>
            <div role="listbox" class="asset-history-picker-list mt-2 max-h-72 overflow-auto">
              <For each={runs()}>{item => <button role="option" aria-selected={run() === item.id} class="asset-history-picker-option block w-full rounded px-2 py-2 text-left text-sm" onClick={() => void selectRun(item)}>
                <span class="block truncate">{runName(item)}</span>
              </button>}</For>
              <Show when={!runs().length}><p class="p-3 text-center text-sm opacity-60">没有匹配的任务记录</p></Show>
            </div>
            <div class="mt-2 flex items-center justify-between text-xs">
              <span>{runTotal() ? `${runOffset() + 1}–${Math.min(runOffset() + RUN_PAGE, runTotal())} / ${runTotal()}` : '0 条'}</span>
              <div class="flex gap-1"><button class="btn btn-xs" disabled={!runOffset()} onClick={() => void loadRuns(Math.max(0, runOffset() - RUN_PAGE))}>上一页</button><button class="btn btn-xs" disabled={runOffset() + RUN_PAGE >= runTotal()} onClick={() => void loadRuns(runOffset() + RUN_PAGE)}>下一页</button></div>
            </div>
          </div>
        </Show>
      </div>
      <button class="btn btn-sm" disabled={loading() || !run()} onClick={() => void loadChanges()}>{loading() ? '读取中…' : '刷新改动'}</button>
    </div>
    <p class="my-2 text-xs opacity-70">{data()?.notice} 仅记录上传文件及公开产物，不包含内部配置。刷新由你控制。</p>
    <Show when={data()?.live}><p class="text-xs">开始前 → 当前；进程中断的记录也可能显示为未结束。</p></Show>
    <Show when={data()?.incomplete}><p role="status">扫描未完整完成，以下列表可能不完整，不能据此认定文件未改动。</p></Show>
    <Show when={error()}><p role="alert">{error()}</p></Show>
    <Show when={run()}>
      <form class="my-3 flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); setPath(''); setChangeOffset(0); void loadChanges(run(), 0); }}>
        <input class="input input-sm min-w-48 flex-1" aria-label="搜索文件改动" placeholder="按文件路径搜索" value={changeQuery()} onInput={e => setChangeQuery(e.currentTarget.value)} />
        <select class="select select-sm" aria-label="筛选改动类型" value={changeStatus()} onChange={e => setChangeStatus(e.currentTarget.value)}>
          <option value="">全部类型</option><option value="added">新增</option><option value="modified">修改</option><option value="deleted">删除</option>
        </select>
        <button class="btn btn-sm" type="submit">筛选</button>
      </form>
    </Show>
    <For each={data()?.changes}>{item => <button class="asset-change-row flex w-full gap-3 border-b p-2 text-left" data-status={item.status} aria-pressed={path() === item.path} onClick={() => void loadDiff(item)}>
      <span class="asset-change-status">{statusName[item.status]}</span><span class="asset-change-path break-all flex-1">{item.path}</span><span class="asset-change-size text-xs">{item.before?.size ?? '—'} → {item.after?.size ?? '—'} B</span>
    </button>}</For>
    <Show when={data() && !data()!.changes.length && !loading()}><p class="py-3">没有匹配的文件变化。</p></Show>
    <Show when={changeTotal() > CHANGE_PAGE}>
      <div class="my-3 flex items-center justify-between text-xs"><span>{changeOffset() + 1}–{Math.min(changeOffset() + CHANGE_PAGE, changeTotal())} / {changeTotal()} 项改动</span>
        <div class="flex gap-1"><button class="btn btn-xs" disabled={!changeOffset()} onClick={() => { setPath(''); void loadChanges(run(), Math.max(0, changeOffset() - CHANGE_PAGE)); }}>上一页</button><button class="btn btn-xs" disabled={changeOffset() + CHANGE_PAGE >= changeTotal()} onClick={() => { setPath(''); void loadChanges(run(), changeOffset() + CHANGE_PAGE); }}>下一页</button></div>
      </div>
    </Show>
    <Show when={path()}><div class="my-3 flex flex-wrap items-center gap-2"><h3 class="asset-change-path mr-auto break-all">{path()} · {selectedChange()?.status === 'added' ? '新增内容' : selectedChange()?.status === 'deleted' ? '删除内容' : '修改前后'}</h3>
      <Show when={props.onPreview && selectedChange()?.after}><button class="btn btn-sm" onClick={() => props.onPreview?.(path())}>打开文件</button></Show>
      <Show when={props.onQuote}><button class="btn btn-sm" onClick={() => props.onQuote?.(`请审查并处理任务改动：${path()}（${statusName[selectedChange()?.status || ''] || selectedChange()?.status}）。`)}>加入主任务</button></Show>
    </div></Show>
    <Show when={selectedChange()}>{item => <details class="asset-change-hashes text-xs opacity-70"><summary>文件版本校验 · SHA-256</summary><p class="break-all">修改前：{item().before?.hash || '未记录 / 不存在'}<br/>修改后：{item().after?.hash || '未记录 / 不存在'}</p></details>}</Show>
    <Show when={data()?.unavailable}><p>{data()?.unavailable}</p></Show>
    <Show when={data()?.diff !== undefined}><pre class="asset-change-diff overflow-auto text-xs p-3 border rounded" style={{ 'max-height': '55vh' }}><For each={data()?.diff?.split('\n')}>{line => <div data-prefix={line[0]} data-header={line.startsWith('+++ ') || line.startsWith('--- ')} style={{ color: line.startsWith('+') ? '#356341' : line.startsWith('-') ? '#993f36' : 'inherit', 'min-height': '1.4em' }}>{line}</div>}</For></pre></Show>
  </section>;
}
