import { createSignal, createEffect, onMount, onCleanup } from 'solid-js';

export const resizedTextareaHeight = (height: number, startY: number, nextY: number, min: number, max: number) =>
  Math.max(min, Math.min(max, height + startY - nextY));
export const composerTextareaHeight=(content:number,min:number,max:number,manual:number|null=null)=>
  Math.min(max,Math.max(min,content,manual??min));

/** Owns resize listeners, height persistence and responsive composer layout. */
export function createComposerLayout(options: {
  textarea: () => HTMLTextAreaElement | undefined;
}) {
  const getViewportMetrics = () => ({
    width: typeof window !== 'undefined' ? window.innerWidth : 1440,
    height: typeof window !== 'undefined' ? window.innerHeight : 900,
  });
  const loadStoredTextareaHeight = () => {
    if (typeof window === 'undefined') return null;
    let raw: string | null;
    try { raw = window.localStorage.getItem('tcode-composer-textarea-height'); } catch { return null; }
    if (!raw) return null;
    const parsed = Number.parseInt(raw, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  };
  const [viewportMetrics, setViewportMetrics] = createSignal(getViewportMetrics());
  const [composerCollapsed, setComposerCollapsed] = createSignal(true);
  const [manualTextareaHeight, setManualTextareaHeight] = createSignal<number | null>(
    loadStoredTextareaHeight(),
  );
  const [isTextareaResizing, setIsTextareaResizing] = createSignal(false);
  let textareaResizeStartY = 0;
  let textareaResizeStartHeight = 0;
  let resizeHandle: HTMLElement | undefined;
  let resizePointerId = 0;
  let previousHeight: number | null = null;

  const isCompactComposer = () => {
    const viewport = viewportMetrics();
    return viewport.height <= 900 || (viewport.width <= 1366 && viewport.height <= 1024);
  };
  const isMinimalComposer = () => composerCollapsed() || isCompactComposer();
  const getTextareaMinHeight = () => (
    44
  );
  const getTextareaMaxHeight = () => {
    const viewport = viewportMetrics();
    const textarea = options.textarea();
    const composer = textarea?.closest<HTMLElement>('[data-composer]');
    const parentHeight = composer?.parentElement?.clientHeight || 0;
    // Reserve visible chat space inside the actual pane, not the whole window.
    const available = parentHeight > 0 && composer && textarea
      ? parentHeight - (composer.offsetHeight - textarea.offsetHeight) - 120
      : Math.floor(viewport.height * .45);
    return Math.max(getTextareaMinHeight(), Math.min(280,Math.floor(viewport.height*.32),available));
  };
  const clampTextareaHeight = (height: number) => (
    Math.max(getTextareaMinHeight(), Math.min(getTextareaMaxHeight(), height))
  );

  const getComposerMaxHeight = () => (
    manualTextareaHeight() ?? getTextareaMinHeight()
  );

  const autoSizeTextarea = (textarea: HTMLTextAreaElement) => {
    textarea.style.minHeight = `${getTextareaMinHeight()}px`;
    textarea.style.maxHeight = `${getTextareaMaxHeight()}px`;
    textarea.style.height = 'auto';
    textarea.style.height = `${composerTextareaHeight(textarea.scrollHeight,getTextareaMinHeight(),getTextareaMaxHeight(),manualTextareaHeight())}px`;
  };

  const toggleComposerCollapsed = () => {
    const textareaRef = options.textarea();
    setComposerCollapsed((prev) => {
      const next = !prev;
      queueMicrotask(() => {
        if (textareaRef) {
          autoSizeTextarea(textareaRef);
        }
      });
      return next;
    });
  };

  const resetManualTextareaHeight = () => {
    const textareaRef = options.textarea();
    setManualTextareaHeight(null);
    if (textareaRef) {
      autoSizeTextarea(textareaRef);
    }
  };

  const resizeTextareaBy = (delta: number) => {
    const textarea = options.textarea();
    setManualTextareaHeight(clampTextareaHeight((textarea?.getBoundingClientRect().height || getTextareaMinHeight()) + delta));
    if (textarea) autoSizeTextarea(textarea);
  };

  const onTextareaPointerMove = (moveEvent: PointerEvent) => {
    const textareaRef = options.textarea();
    if (!isTextareaResizing()) return;
    if (moveEvent.pointerId !== resizePointerId) return;
    const nextHeight = resizedTextareaHeight(textareaResizeStartHeight, textareaResizeStartY, moveEvent.clientY, getTextareaMinHeight(), getTextareaMaxHeight());
    setManualTextareaHeight(nextHeight);
    if (textareaRef) {
      autoSizeTextarea(textareaRef);
    }
  };

  const onTextareaPointerUp = () => {
    if (!isTextareaResizing()) return;
    setIsTextareaResizing(false);
    if (resizeHandle?.hasPointerCapture(resizePointerId)) resizeHandle.releasePointerCapture(resizePointerId);
    resizeHandle = undefined;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  };

  const cancelTextareaResize = () => {
    if (!isTextareaResizing()) return;
    setManualTextareaHeight(previousHeight);
    const textarea = options.textarea();
    if (textarea) autoSizeTextarea(textarea);
    onTextareaPointerUp();
  };

  const startTextareaResize = (event: PointerEvent) => {
    const textareaRef = options.textarea();
    if (event.button !== 0) return;
    event.preventDefault();
    previousHeight = manualTextareaHeight();
    resizeHandle = event.currentTarget as HTMLElement;
    resizePointerId = event.pointerId;
    resizeHandle.focus();
    resizeHandle.setPointerCapture(resizePointerId);
    textareaResizeStartY = event.clientY;
    textareaResizeStartHeight = textareaRef
      ? textareaRef.getBoundingClientRect().height
      : clampTextareaHeight(manualTextareaHeight() ?? getComposerMaxHeight());
    setIsTextareaResizing(true);
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
  };

  createEffect(() => {
    const height = manualTextareaHeight();
    if (typeof window === 'undefined') return;
    try {
      if (height === null) window.localStorage.removeItem('tcode-composer-textarea-height');
      else window.localStorage.setItem('tcode-composer-textarea-height', String(Math.round(height)));
    } catch { /* The input must remain usable when browser storage is unavailable. */ }
  });

  createEffect(() => {
    composerCollapsed();
    viewportMetrics();
    queueMicrotask(() => {
      const textareaRef = options.textarea();
      if (textareaRef) {
        autoSizeTextarea(textareaRef);
      }
    });
  });

  onMount(() => {
    const syncViewportMetrics = () => {
      const textareaRef = options.textarea();
      setViewportMetrics(getViewportMetrics());
      setManualTextareaHeight((prev) => (prev === null ? null : clampTextareaHeight(prev)));
      if (textareaRef) {
        autoSizeTextarea(textareaRef);
      }
    };
    window.addEventListener('resize', syncViewportMetrics);
    window.addEventListener('pointermove', onTextareaPointerMove);
    window.addEventListener('pointerup', onTextareaPointerUp);
    window.addEventListener('pointercancel', cancelTextareaResize);
    window.addEventListener('blur', cancelTextareaResize);
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') cancelTextareaResize(); };
    window.addEventListener('keydown', escape);
    const parent = options.textarea()?.closest('[data-composer]')?.parentElement;
    const observer = new ResizeObserver(syncViewportMetrics);
    if (parent) observer.observe(parent);
    onCleanup(() => {
      observer.disconnect();
      onTextareaPointerUp();
      window.removeEventListener('resize', syncViewportMetrics);
      window.removeEventListener('pointermove', onTextareaPointerMove);
      window.removeEventListener('pointerup', onTextareaPointerUp);
      window.removeEventListener('pointercancel', cancelTextareaResize);
      window.removeEventListener('blur', cancelTextareaResize);
      window.removeEventListener('keydown', escape);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    });
  });

  return { manualTextareaHeight, composerCollapsed, isCompactComposer, isMinimalComposer, isTextareaResizing, autoSizeTextarea, toggleComposerCollapsed, resetManualTextareaHeight, startTextareaResize, resizeTextareaBy, getTextareaMinHeight, getTextareaMaxHeight };
}
