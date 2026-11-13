import { For, Show, createEffect } from 'solid-js';
import type { CommandItem } from './slashCommands';
import './SlashCommandMenu.css';

export default function SlashCommandMenu(props: {
  title: string; query: string; secondary: boolean; items: CommandItem[];
  index: number; loading: boolean; error: string;
  onQuery: (query: string) => void; onHighlight: (index: number) => void;
  onSelect: (item: CommandItem) => void; onBack: () => void;
  onKeyDown: (event: KeyboardEvent) => void; onManageSkills?: () => void;
}) {
  let root: HTMLDivElement | undefined;
  createEffect(() => {
    const index = props.index;
    root?.querySelector(`[data-command-index="${index}"]`)?.scrollIntoView({ block: 'nearest' });
  });
  return (
    <div ref={root} data-testid="slash-command-menu" class="command-menu">
      <div class="command-menu-header">
        <Show when={props.secondary}><button type="button" aria-label="返回命令列表" class="command-menu-control" onClick={props.onBack}>←</button></Show>
        <span class="command-menu-title">{props.title}</span>
        <Show when={props.onManageSkills}><button type="button" class="command-menu-control" onClick={props.onManageSkills}>管理技能</button></Show>
        <span class="command-menu-escape"><kbd>Esc</kbd> {props.secondary ? '返回' : '关闭'}</span>
      </div>
      <Show when={props.secondary}>
        <input ref={element => queueMicrotask(() => element.focus())} aria-label={`搜索${props.title}`} value={props.query}
          onInput={event => props.onQuery(event.currentTarget.value)} onKeyDown={props.onKeyDown}
          class="command-menu-search" placeholder="输入名称搜索…" />
      </Show>
      <div role="listbox" aria-label={props.title} class="command-menu-list">
        <Show when={props.error}><div role="alert" class="command-menu-empty command-menu-error">{props.error}</div></Show>
        <Show when={props.loading}><div role="status" class="command-menu-empty">正在加载…</div></Show>
        <Show when={!props.loading && !props.items.length && !props.error}><div class="command-menu-empty">没有匹配项</div></Show>
        <For each={props.items}>{(item, index) => (
          <button type="button" role="option" aria-selected={props.index === index()} aria-disabled={Boolean(item.disabled)}
            data-command-index={index()} class="command-menu-option"
            onMouseEnter={() => props.onHighlight(index())} onMouseDown={event => event.preventDefault()} onClick={() => props.onSelect(item)}>
            <span aria-hidden="true" class="command-menu-icon">
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <Show when={item.kind === 'skill'} fallback={<Show when={item.kind === 'model'} fallback={<path d="m15 5-6 14" />}><rect x="5" y="5" width="14" height="14" rx="3" /><path d="M9 9h6v6H9zM9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3" /></Show>}><path d="m12 3 9 9-9 9-9-9zM8 12h8m-4-4v8" /></Show>
              </svg>
            </span>
            <div class="command-menu-copy">
              <div class="command-menu-label">{item.label}</div>
              <div class="command-menu-description">{item.disabled || item.description}</div>
            </div>
            <span class="command-menu-state">
              <Show when={item.current}><span class="command-menu-current">✓ 当前</span></Show>
              <Show when={props.index === index() && !item.disabled}><kbd aria-hidden="true">↵</kbd></Show>
              <Show when={item.disabled}><span class="command-menu-unavailable">不可用</span></Show>
            </span>
          </button>
        )}</For>
      </div>
      <div class="command-menu-footer"><span><kbd>↑</kbd> <kbd>↓</kbd> 选择</span><span><kbd>Enter</kbd> 确认</span><span><kbd>Tab</kbd> 补全</span></div>
      <Show when={props.title.startsWith('命令帮助')}><div class="command-menu-help">
        <div>/model provider/model-id · 直接切换模型</div>
        <div>/skill 技能名 任务内容 · 指定本次技能</div>
        <div>输入 $ 搜索技能；选择后填写任务并发送。</div>
      </div></Show>
    </div>
  );
}
