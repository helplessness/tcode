import { createEffect, createSignal, createUniqueId, onCleanup, onMount, Show, type JSX } from 'solid-js';

/** Visual clipping only: the original message remains available to copy/edit. */
export default function UserMessageText(props: { enabled: boolean; contentKey: string; children: JSX.Element }) {
  if (!props.enabled) return props.children;
  const [expanded, setExpanded] = createSignal(false);
  const [long, setLong] = createSignal(false);
  const id = createUniqueId();
  let viewport!: HTMLDivElement;
  let content!: HTMLDivElement;
  const measure = () => {
    const sample = content.querySelector('p, pre, li') || content;
    const limit = Math.round((parseFloat(getComputedStyle(sample).lineHeight) || 24) * 8);
    viewport.style.setProperty('--user-message-preview-height', `${limit}px`);
    setLong(content.getBoundingClientRect().height > limit + 1);
  };
  createEffect(() => {
    props.contentKey;
    setExpanded(false);
    queueMicrotask(() => { if (viewport?.isConnected) { viewport.scrollTop = 0; measure(); } });
  });
  onMount(() => {
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    measure();
    onCleanup(() => observer.disconnect());
  });
  const toggle = () => {
    setExpanded(value => !value);
    viewport.scrollTop = 0;
  };
  return <div class="user-message-bounded">
    <div ref={viewport} id={id} class="user-message-text" classList={{ 'is-long': long(), 'is-expanded': expanded() }}
      tabIndex={long() && expanded() ? 0 : undefined} role={long() && expanded() ? 'region' : undefined}
      aria-label={long() && expanded() ? '用户消息全文，可滚动' : undefined}
      onFocusIn={() => { if (long() && !expanded()) setExpanded(true); }}>
      <div ref={content}>{props.children}</div>
    </div>
    <Show when={long()}><button type="button" class="user-message-expand" aria-expanded={expanded()} aria-controls={id} onClick={toggle}>
      {expanded() ? '收起' : '展开全文'}
      <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style={{transform:expanded()?'rotate(180deg)':undefined}}><path d="m6 9 6 6 6-6"/></svg>
    </button></Show>
  </div>;
}
