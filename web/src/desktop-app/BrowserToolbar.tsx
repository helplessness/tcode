import { confirmAction } from '../components/FeedbackDialog';
import { For, Show, createSignal, onMount, onCleanup } from 'solid-js';
import type { BrowserCommand, BrowserState } from '../desktop';
import './browser.css';

export default function BrowserToolbar(props: { state: BrowserState; command: (command: BrowserCommand) => Promise<void> }) {
  const [section, setSection] = createSignal('');
  const [address, setAddress] = createSignal<string | null>(null);
  const [query, setQuery] = createSignal('');
  const navigate = (url: string) => {
    setAddress(null); setSection('');
    void props.command({ action: 'navigate', url: /^https?:\/\//i.test(url) ? url : `https://${url}` });
  };
  const run = (action: BrowserCommand['action']) => { void props.command({ action }); };
  onMount(() => {
    const select = (event: Event) => setSection((event as CustomEvent<string>).detail);
    window.addEventListener('tcode:browser-menu-action', select);
    onCleanup(() => window.removeEventListener('tcode:browser-menu-action', select));
  });
  return <div class="desktop-browser-tools" aria-busy={props.state.loading}>
    <form class="desktop-browser-address" onSubmit={event => { event.preventDefault(); const url = (address() ?? props.state.url).trim(); if (url) navigate(url); }}>
      <button type="button" aria-label="后退" disabled={props.state.loading || !props.state.canGoBack} onClick={() => run('back')}>←</button>
      <button type="button" aria-label="前进" disabled={props.state.loading || !props.state.canGoForward} onClick={() => run('forward')}>→</button>
      <button type="button" aria-label="刷新" disabled={props.state.loading} onClick={() => run('reload')}>↻</button>
      <input aria-label="浏览器网址" value={address() ?? props.state.url} onInput={event => setAddress(event.currentTarget.value)} placeholder="输入网址" />
      <button type="submit" disabled={props.state.loading}>{props.state.loading ? '加载中' : '前往'}</button>
      <button type="button" aria-label="浏览器更多选项" aria-haspopup="menu" onClick={event => {
        const rect = event.currentTarget.getBoundingClientRect();
        void props.command({ action: 'menu', bounds: { x: rect.left, y: rect.bottom, width: rect.width, height: rect.height } });
      }}>•••</button>
    </form>
    <Show when={props.state.loading}><div class="desktop-browser-loading" role="status" aria-live="polite"><span />正在加载页面，请稍候…</div></Show>
    <Show when={section()}><section class="desktop-browser-section">
      <header><b>{{ find: '在页面中查找', history: '历史记录', downloads: '下载', settings: '浏览器设置' }[section()]}</b><button aria-label="关闭浏览器选项" onClick={() => { if (section() === 'find') void props.command({ action: 'find', text: '' }); setSection(''); }}>×</button></header>
      <Show when={section() === 'find'}><form onSubmit={event => { event.preventDefault(); void props.command({ action: 'find', text: query() }); }}><input aria-label="查找文本" value={query()} onInput={event => setQuery(event.currentTarget.value)} /><button type="button" onClick={() => void props.command({ action: 'find', text: query(), forward: false })}>上一个</button><button type="submit">下一个</button></form></Show>
      <Show when={section() === 'history'}><p>当前浏览器会话的历史记录</p><For each={props.state.history || []}>{item => <button class="desktop-browser-record" title={item.url} onClick={() => navigate(item.url)}>{item.title || item.url}</button>}</For><Show when={!props.state.history?.length}><p>暂无历史记录</p></Show></Show>
      <Show when={section() === 'downloads'}><For each={props.state.downloads || []}>{item => <div class="desktop-browser-record">{item.name}<span>{item.state === 'completed' ? '已完成' : item.state === 'progressing' ? `${Math.round(item.received / Math.max(1, item.total) * 100)}%` : '已中断或取消'}</span><Show when={item.state === 'completed'}><button onClick={() => void props.command({ action: 'reveal-download', id: item.id })}>在文件夹中显示</button></Show></div>}</For><Show when={!props.state.downloads?.length}><p>暂无下载</p></Show></Show>
      <Show when={section() === 'settings'}><label><input type="checkbox" checked={props.state.downloadsEnabled} onChange={event => void props.command({ action: 'downloads-enabled', enabled: event.currentTarget.checked })} />允许手动下载（每次选择保存位置）</label><p>浏览器数据与主程序隔离；Agent 操作期间禁止下载。历史记录仅保留在当前浏览器会话。</p><button onClick={async () => { if (await confirmAction('清除当前浏览器的 Cookie、缓存和历史记录？登录状态也会被清除。')) run('clear-data'); }}>清除浏览数据</button><button onClick={() => run('fit')}>适应宽度</button></Show>
    </section></Show>
  </div>;
}
