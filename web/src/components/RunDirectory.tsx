import { createMemo, createSignal, createUniqueId, For, Show } from 'solid-js';
import { chatRunDirectory, runDirectoryLabels, type PresentedChatTurn } from '../utils/chatPresentation';
import './RunDirectory.css';

const filters = [['all', '全部'], ['failed', '失败'], ['interrupted', '中断']] as const;
const timeFormat = new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

export default function RunDirectory(props: {
  turns: PresentedChatTurn[];
  disabled: boolean;
  onNavigate: (messageId?: string) => void;
}) {
  let dialog!: HTMLDialogElement;
  let input!: HTMLInputElement;
  const id = createUniqueId();
  const [open, setOpen] = createSignal(false);
  const [query, setQuery] = createSignal('');
  const [filter, setFilter] = createSignal('all');
  const rows = createMemo(() => open() ? chatRunDirectory(props.turns, query(), filter()) : []);
  const go = (messageId?: string) => {
    dialog.close();
    props.onNavigate(messageId);
  };
  return <>
    <button type="button" class="run-directory-trigger" disabled={props.disabled} aria-haspopup="dialog"
      onClick={() => { setQuery(''); setFilter('all'); setOpen(true); dialog.showModal(); input.focus(); }}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/></svg>
      运行目录
    </button>
    <dialog ref={dialog} class="run-directory" aria-labelledby={id}
      onClose={() => setOpen(false)}
      onKeyDown={event => event.stopPropagation()}
      onClick={event => { if (event.target === dialog) dialog.close(); }}>
      <div class="run-directory-panel">
        <header>
          <div><p>当前会话</p><h2 id={id}>运行目录</h2></div>
          <button type="button" class="run-directory-close" aria-label="关闭运行目录" onClick={() => dialog.close()}>
            <svg width="18" height="18" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8" fill="none" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
          </button>
        </header>
        <div class="run-directory-controls">
          <input ref={input} type="search" aria-label="搜索当前会话运行" placeholder="搜索指令或回复…" value={query()} onInput={event => setQuery(event.currentTarget.value)} />
          <div class="run-directory-filters" role="group" aria-label="筛选运行状态">
            <For each={filters}>{([value, label]) => <button type="button" aria-pressed={filter() === value} onClick={() => setFilter(value)}>{label}</button>}</For>
          </div>
        </div>
        <div class="run-directory-list">
          <Show when={rows().length} fallback={<p class="run-directory-empty">{query().trim() || filter() !== 'all' ? '没有符合条件的运行' : '发送指令后，运行会出现在这里。'}</p>}>
            <ol><For each={rows()}>{row => {
              const stamp = Number(row.timestamp || 0);
              const date = new Date(stamp < 1e12 ? stamp * 1000 : stamp);
              return <li><button type="button" class="run-directory-row" onClick={() => go(row.id)}>
                <span class="run-directory-index">#{row.index}</span>
                <span class="run-directory-copy"><strong>{row.summary}</strong>
                  <Show when={stamp > 0 && Number.isFinite(date.getTime())} fallback={<span>时间未记录</span>}>
                    <time dateTime={date.toISOString()}>{timeFormat.format(date)}</time>
                  </Show>
                </span>
                <span class="run-directory-status" data-status={row.status}>{runDirectoryLabels[row.status]}</span>
              </button></li>;
            }}</For></ol>
          </Show>
        </div>
        <footer><span role="status">{rows().length} 条运行 · 最近优先</span>
          <button type="button" disabled={!props.turns.length} onClick={() => go()}>回到最新 ↓</button></footer>
      </div>
    </dialog>
  </>;
}
