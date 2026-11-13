import { For, Show, createEffect, createMemo, createSignal, onCleanup, onMount } from 'solid-js';
import { searchLabels, searchMatches, searchWorkspace, type SearchHit } from '../api/globalSearch';
import './CommandPalette.css';

export type CommandItem = { id: string; label: string; detail: string; keywords?: string; run: () => void };

export default function CommandPalette(props: { items: CommandItem[]; workspaceId?: string; onHit: (hit: SearchHit) => void | Promise<void>; onClose: () => void }) {
  const [query, setQuery] = createSignal('');
  const [selected, setSelected] = createSignal(0);
  const [hits, setHits] = createSignal<SearchHit[]>([]);
  const [warnings, setWarnings] = createSignal<string[]>([]);
  const [loading, setLoading] = createSignal(false);
  const [archived, setArchived] = createSignal(false);
  const [category, setCategory] = createSignal('all');
  const [retry, setRetry] = createSignal(0);
  let input!: HTMLInputElement;
  let panel!: HTMLElement;
  const previousFocus = document.activeElement as HTMLElement | null;
  const results = createMemo(() => [
    ...hits().filter(hit => category() === 'all' || category() === hit.kind).map(hit => ({
      id: `${hit.kind}:${hit.id}`, label: hit.title,
      detail: `${searchLabels[hit.kind]}${hit.archived ? ' · 已归档' : ''} · ${hit.snippet}`,
      run: () => props.onHit(hit),
    })),
    ...(category() === 'all' || category() === 'command' ? props.items.filter(item =>
      searchMatches(query(), item.label, item.detail, item.keywords)).map(item => ({...item, detail: `功能 · ${item.detail}`})) : []),
  ]);
  createEffect(() => {
    const text = query().trim(), workspace = props.workspaceId || '', includeArchived = archived();
    void retry();
    setHits([]); setWarnings([]); setSelected(0);
    if (!text || !workspace) { setLoading(false); return; }
    const controller = new AbortController();
    let disposed = false;
    setLoading(true);
    const timer = window.setTimeout(async () => {
      const timeout = window.setTimeout(() => controller.abort(), 20000);
      try {
        const data = await searchWorkspace(text, workspace, controller.signal, includeArchived, 50, data => {
          if (!disposed) {setHits(data.hits);setWarnings(data.warnings);}
        });
        if (!disposed) { setHits(data.hits); setWarnings(data.warnings); }
      } catch {
        if (!disposed) setWarnings([controller.signal.aborted ? '搜索超时，请细化关键词后重试' : '搜索失败，请重试']);
      } finally {
        window.clearTimeout(timeout);
        if (!disposed) setLoading(false);
      }
    }, 250);
    onCleanup(() => { disposed = true; window.clearTimeout(timer); controller.abort(); });
  });
  createEffect(() => {
    const index = Math.max(0, Math.min(selected(), results().length - 1));
    if (index !== selected()) setSelected(index);
    panel?.querySelector(`[data-result-index="${index}"]`)?.scrollIntoView({block: 'nearest'});
  });
  const choose=(item:CommandItem|undefined)=>{if(item){props.onClose();item.run();}};
  onMount(()=>input.focus());
  onCleanup(() => { if (previousFocus?.isConnected) previousFocus.focus(); });
  return <div class="command-palette-backdrop" role="presentation" onPointerDown={event=>{if(event.target===event.currentTarget)props.onClose();}}><section ref={panel} class="command-palette" role="dialog" aria-modal="true" aria-label="全局搜索" onKeyDown={event=>{
    if (event.isComposing) return;
    if(event.key==='Escape'){event.preventDefault();props.onClose();}
    if(event.target===input && event.key==='ArrowDown'){event.preventDefault();setSelected(Math.min(results().length-1,selected()+1));}
    if(event.target===input && event.key==='ArrowUp'){event.preventDefault();setSelected(Math.max(0,selected()-1));}
    if(event.target===input && event.key==='Enter'){event.preventDefault();choose(results()[selected()]);}
    if (event.key === 'Tab') {
      const controls = [...panel.querySelectorAll<HTMLElement>('input, select, button:not([disabled])')];
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }}><header><span>⌘K</span><input ref={input} type="search" maxlength={200} value={query()} onInput={event=>{setQuery(event.currentTarget.value);setSelected(0);}} placeholder="搜索任务、聊天、文件、Run、文献或功能" aria-label="搜索当前工作区" /><button onClick={props.onClose} aria-label="关闭搜索">×</button></header>
  <div class="px-4 py-2 flex flex-wrap gap-3 text-xs"><span>当前工作区</span><select aria-label="搜索分类" value={category()} onChange={event => {setCategory(event.currentTarget.value);setSelected(0);}}><option value="all">全部</option><option value="command">功能与操作</option><For each={Object.entries(searchLabels)}>{([key,label])=><option value={key}>{label}</option>}</For></select><label><input type="checkbox" checked={archived()} onChange={event=>setArchived(event.currentTarget.checked)}/> 包含归档会话、课题与 Wiki</label></div>
  <div class="command-palette-results" aria-busy={loading()}><Show when={loading()}><p role="status">正在搜索工作区…</p></Show><For each={results()}>{(item,index)=><button data-result-index={index()} data-selected={index()===selected()} onFocus={()=>setSelected(index())} onMouseEnter={()=>setSelected(index())} onClick={()=>choose(item)}><b>{item.label}</b><span>{item.detail}</span></button>}</For><Show when={!loading() && !results().length}><p role="status">没有匹配结果，试试标题、文件名、运行 ID 或正文关键词。</p></Show><Show when={warnings().length}><div role="status" class="px-4 py-2 text-xs"><For each={warnings()}>{warning=><p>{warning}</p>}</For><button onClick={()=>setRetry(value=>value+1)}>重试搜索</button></div></Show><Show when={!props.workspaceId}><p>请先选择工作区，当前仅搜索功能与操作。</p></Show></div><footer>↑↓ 选择　Enter 打开　Esc 关闭 · 文件按名称／路径搜索，不搜索文件正文</footer></section></div>;
}
