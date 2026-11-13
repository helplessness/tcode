import { For, Show, createEffect, createSignal, onCleanup, onMount } from 'solid-js';
import { Portal } from 'solid-js/web';

type Tool = 'select' | 'pen' | 'text' | 'rect' | 'ellipse' | 'eraser';
type Point = { x: number; y: number };
type Shape = { tool: Tool; points: Point[]; color: string; width: number; text?: string };
const WIDTH = 1440;
const HEIGHT = 900;
const COLORS = ['#000000', '#6b7280', '#92400e', '#dc2626', '#f97316', '#f59e0b', '#16a34a', '#0d9488', '#06b6d4', '#2563eb', '#4f46e5', '#9333ea', '#db2777'];
const TOOLS: { tool: Tool; label: string; icon: string }[] = [
  { tool: 'select', label: '选择', icon: 'M5 3l14 9-7 1-3 7z' },
  { tool: 'pen', label: '画笔', icon: 'M3 17C8 10 13 2 15 4S5 20 10 20s9-14 11-10-5 8-1 9' },
  { tool: 'text', label: '文字', icon: 'M5 5h14M12 5v15M8 20h8' },
  { tool: 'rect', label: '矩形', icon: 'M4 5h16v14H4z' },
  { tool: 'ellipse', label: '圆形', icon: 'M12 3a9 9 0 1 0 0 18 9 9 0 1 0 0-18' },
  { tool: 'eraser', label: '橡皮擦', icon: 'M3 14l10-10 8 8-9 9H9zM7 10l8 8' },
];

function bounds(shape: Shape) {
  const xs = shape.points.map(p => p.x);
  const ys = shape.points.map(p => p.y);
  const x = Math.min(...xs), y = Math.min(...ys);
  return { x, y, w: shape.tool === 'text' ? Math.max(40, ...(shape.text || '').split('\n').map(s => s.length * shape.width * 3)) : Math.max(...xs) - x,
    h: shape.tool === 'text' ? (shape.text || '').split('\n').length * shape.width * 7 : Math.max(...ys) - y };
}

export default function SketchPanel(props: { onClose: () => void; onComplete: (file: File) => Promise<void> }) {
  let panel!: HTMLDivElement;
  let canvas!: HTMLCanvasElement;
  let textInput: HTMLTextAreaElement | undefined;
  const [tool, setTool] = createSignal<Tool>('pen');
  const [color, setColor] = createSignal('#000000');
  const [width, setWidth] = createSignal(4);
  const [history, setHistory] = createSignal<Shape[][]>([[]]);
  const [position, setPosition] = createSignal(0);
  const [selected, setSelected] = createSignal(-1);
  const [editing, setEditing] = createSignal<{ point: Point; text: string; index: number; color: string; width: number } | null>(null);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal('');
  const shapes = () => history()[position()];
  let gesture: { start: Point; shape?: Shape; index?: number; original: Shape[]; moved: boolean; resize?: boolean; pointerId: number } | null = null;
  const clone = (items: Shape[]) => items.map(s => ({ ...s, points: s.points.map(p => ({ ...p })) }));
  const commit = (items: Shape[]) => {
    setHistory([...history().slice(0, position() + 1), clone(items)]);
    setPosition(position() + 1);
    setError('');
  };
  const finishText = () => {
    const value = editing();
    if (!value) return;
    setEditing(null);
    const items = clone(shapes());
    if (value.text.trim()) {
      const shape: Shape = { tool: 'text', points: [value.point], text: value.text, color: value.color, width: value.width };
      if (value.index >= 0) items[value.index] = shape; else items.push(shape);
      commit(items);
    }
  };
  const undo = () => { finishText(); if (position() > 0) setPosition(position() - 1); setSelected(-1); };
  const redo = () => { if (position() < history().length - 1) setPosition(position() + 1); setSelected(-1); };

  const paint = (items: Shape[], selection = true) => {
    const ctx = canvas?.getContext('2d');
    if (!ctx) return;
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, WIDTH, HEIGHT);
    for (const shape of items) {
      ctx.strokeStyle = shape.tool === 'eraser' ? '#ffffff' : shape.color;
      ctx.fillStyle = shape.color;
      ctx.lineWidth = shape.tool === 'eraser' ? shape.width * 5 : shape.width;
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      const first = shape.points[0], last = shape.points[shape.points.length - 1];
      ctx.beginPath();
      if (shape.tool === 'text') {
        ctx.font = `${shape.width * 6}px sans-serif`; ctx.textBaseline = 'top';
        (shape.text || '').split('\n').forEach((line, i) => ctx.fillText(line, first.x, first.y + i * shape.width * 7));
      } else if (shape.tool === 'rect') {
        ctx.strokeRect(first.x, first.y, last.x - first.x, last.y - first.y);
      } else if (shape.tool === 'ellipse') {
        ctx.ellipse((first.x + last.x) / 2, (first.y + last.y) / 2, Math.abs(last.x - first.x) / 2, Math.abs(last.y - first.y) / 2, 0, 0, Math.PI * 2); ctx.stroke();
      } else {
        ctx.moveTo(first.x, first.y);
        if (shape.points.length === 1) ctx.lineTo(first.x + 0.1, first.y);
        for (const point of shape.points.slice(1)) ctx.lineTo(point.x, point.y);
        ctx.stroke();
      }
    }
    if (selection && selected() >= 0 && items[selected()]) {
      const b = bounds(items[selected()]);
      ctx.strokeStyle = '#2563eb'; ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
      ctx.strokeRect(b.x - 8, b.y - 8, b.w + 16, b.h + 16); ctx.setLineDash([]);
      ctx.fillStyle = '#2563eb'; ctx.fillRect(b.x + b.w + 3, b.y + b.h + 3, 10, 10);
    }
  };
  createEffect(() => { const items = shapes(); selected(); paint(items); });
  const pointAt = (event: PointerEvent | MouseEvent): Point => {
    const rect = canvas.getBoundingClientRect();
    return { x: Math.max(0, Math.min(WIDTH, (event.clientX - rect.left) * WIDTH / rect.width)), y: Math.max(0, Math.min(HEIGHT, (event.clientY - rect.top) * HEIGHT / rect.height)) };
  };
  const hit = (point: Point) => {
    for (let i = shapes().length - 1; i >= 0; i--) {
      if (shapes()[i].tool === 'eraser') continue;
      const b = bounds(shapes()[i]);
      if (point.x >= b.x - 10 && point.x <= b.x + b.w + 10 && point.y >= b.y - 10 && point.y <= b.y + b.h + 10) return i;
    }
    return -1;
  };
  const beginText = (point: Point, index = -1) => {
    const existing = shapes()[index];
    const textWidth = Math.max(width(), 18 * WIDTH / canvas.getBoundingClientRect().width / 6);
    setEditing({ point: existing?.points[0] || point, text: existing?.text || '', index, color: existing?.color || color(), width: existing?.width || textWidth });
    queueMicrotask(() => textInput?.focus());
  };
  const down = (event: PointerEvent) => {
    if (busy() || event.button !== 0 || gesture) return;
    event.preventDefault();
    finishText();
    const point = pointAt(event);
    if (tool() === 'text') { const index = hit(point); beginText(point, index >= 0 && shapes()[index].tool === 'text' ? index : -1); return; }
    canvas.setPointerCapture(event.pointerId);
    const original = clone(shapes());
    if (tool() === 'select') {
      const current = selected() >= 0 ? shapes()[selected()] : undefined;
      const b = current && bounds(current);
      const resize = Boolean(b && Math.abs(point.x - b.x - b.w - 8) < 16 && Math.abs(point.y - b.y - b.h - 8) < 16);
      const index = resize ? selected() : hit(point);
      setSelected(index);
      gesture = { start: point, index, original, moved: false, resize, pointerId: event.pointerId };
    } else {
      setSelected(-1);
      gesture = { start: point, original, moved: false, pointerId: event.pointerId, shape: { tool: tool(), color: color(), width: width(), points: [point] } };
      paint([...original, gesture.shape!]);
    }
  };
  const gestureItems = (point: Point) => {
    if (!gesture) return shapes();
    if (gesture.shape) {
      if (gesture.shape.tool === 'rect' || gesture.shape.tool === 'ellipse') gesture.shape.points = [gesture.start, point];
      else gesture.shape.points.push(point);
      return [...gesture.original, gesture.shape];
    }
    const items = clone(gesture.original), index = gesture.index ?? -1;
    if (index >= 0) {
      const shape = items[index], b = bounds(shape);
      if (gesture.resize) {
        const sx = Math.max(0.1, (point.x - b.x) / Math.max(1, b.w));
        const sy = Math.max(0.1, (point.y - b.y) / Math.max(1, b.h));
        shape.points = shape.points.map(p => ({ x: b.x + (p.x - b.x) * sx, y: b.y + (p.y - b.y) * sy }));
        if (shape.tool === 'text') shape.width *= Math.max(sx, sy);
      } else shape.points = shape.points.map(p => ({ x: p.x + point.x - gesture!.start.x, y: p.y + point.y - gesture!.start.y }));
    }
    return items;
  };
  const move = (event: PointerEvent) => {
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    gesture.moved = true; paint(gestureItems(pointAt(event)));
  };
  const up = (event: PointerEvent, cancelled = false) => {
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    const items = gestureItems(pointAt(event));
    const changed = gesture.shape || (gesture.moved && (gesture.index ?? -1) >= 0);
    gesture = null;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    if (changed && !cancelled) commit(items); else paint(shapes());
  };
  const save = async () => {
    finishText();
    if (!shapes().length || busy()) return;
    setBusy(true); setError(''); paint(shapes(), false);
    try {
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(b => b ? resolve(b) : reject(new Error('无法导出图片')), 'image/png'));
      await props.onComplete(new File([blob], `sketch-${Date.now()}.png`, { type: 'image/png' }));
      props.onClose();
    } catch (e) { setError(e instanceof Error ? e.message : '图片上传失败，请重试'); }
    finally { setBusy(false); paint(shapes()); }
  };
  onMount(() => {
    paint(shapes());
    const previous = document.body.style.overflow;
    const previousFocus = document.activeElement as HTMLElement | null;
    panel.querySelector<HTMLButtonElement>('button')?.focus();
    document.body.style.overflow = 'hidden';
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Tab') {
        const controls = Array.from(panel.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea'));
        const index = controls.indexOf(document.activeElement as HTMLElement);
        if (event.shiftKey ? index <= 0 : index === controls.length - 1 || index < 0) {
          event.preventDefault(); controls[event.shiftKey ? controls.length - 1 : 0]?.focus();
        }
      }
      if (busy()) return;
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); event.shiftKey ? redo() : undo(); }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') { event.preventDefault(); redo(); }
      if ((event.key === 'Delete' || event.key === 'Backspace') && selected() >= 0) { event.preventDefault(); commit(shapes().filter((_, i) => i !== selected())); setSelected(-1); }
      if (event.key === 'Escape') { if (editing()) setEditing(null); else props.onClose(); }
    };
    window.addEventListener('keydown', key);
    onCleanup(() => { document.body.style.overflow = previous; window.removeEventListener('keydown', key); previousFocus?.focus(); });
  });
  return <Portal>
    <div ref={panel} class="sketch-panel" role="dialog" aria-modal="true" aria-label="绘制草图">
      <button class="sketch-close sketch-button" aria-label="关闭绘图面板" disabled={busy()} onClick={props.onClose}>×</button>
      <div class="sketch-toolbar" role="toolbar" aria-label="绘图工具">
        <For each={TOOLS}>{item => <button class={`sketch-button ${tool() === item.tool ? 'is-active' : ''}`} title={item.label} aria-label={item.label} aria-pressed={tool() === item.tool} disabled={busy()} onClick={() => { finishText(); setTool(item.tool); setSelected(-1); }}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d={item.icon} /></svg>
        </button>}</For>
      </div>
      <div class="sketch-history">
        <button class="sketch-button" title="撤销（Ctrl / ⌘ Z）" aria-label="撤销" disabled={busy() || position() === 0} onClick={undo}>↶</button>
        <button class="sketch-button" title="重做（Ctrl / ⌘ Shift Z）" aria-label="重做" disabled={busy() || position() === history().length - 1} onClick={redo}>↷</button>
      </div>
      <div class="sketch-surface">
        <div class="sketch-board">
        <canvas ref={canvas} width={WIDTH} height={HEIGHT} aria-label="绘图画布" style={{ cursor: tool() === 'select' ? 'default' : tool() === 'text' ? 'text' : 'crosshair' }} onPointerDown={down} onPointerMove={move} onPointerUp={event => up(event)} onPointerCancel={event => up(event, true)} onDblClick={event => { if (tool() === 'select') { const index = hit(pointAt(event)); if (index >= 0 && shapes()[index].tool === 'text') beginText(pointAt(event), index); } }} />
        <Show when={editing()}>{value => <textarea ref={textInput} aria-label="草图文字" class="sketch-text" style={{ left: `${value().point.x / WIDTH * 100}%`, top: `${value().point.y / HEIGHT * 100}%`, color: value().color, 'font-size': `${value().width * 6 * canvas.getBoundingClientRect().width / WIDTH}px` }} value={value().text} onInput={event => setEditing({ ...value(), text: event.currentTarget.value })} onBlur={finishText} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setEditing(null); } if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); finishText(); } }} placeholder="输入文字" />}</Show>
        </div>
      </div>
      <label class="sketch-line-width"><span>线宽 {width()}</span><input type="range" min="1" max="16" value={width()} aria-label="线宽" disabled={busy()} onInput={event => setWidth(Number(event.currentTarget.value))} /><span class="sketch-width-dot" style={{ width: `${width() + 4}px`, height: `${width() + 4}px`, background: color() }} /></label>
      <div class="sketch-palette" role="group" aria-label="画笔颜色">
        <label class="sketch-custom-color" title="自定义颜色"><input type="color" aria-label="自定义颜色" value={color()} disabled={busy()} onInput={event => setColor(event.currentTarget.value)} /></label>
        <For each={COLORS}>{c => <button class={`sketch-color ${color() === c ? 'is-active' : ''}`} style={{ background: c }} aria-label={`颜色 ${c}`} aria-pressed={color() === c} disabled={busy()} onClick={() => setColor(c)} />}</For>
      </div>
      <div class="sketch-hint" role="status">{error() || (busy() ? '正在添加图片…' : tool() === 'select' ? '拖动对象移动 · 拖动右下角缩放 · 双击编辑文字 · Delete 删除' : tool() === 'text' ? '点击画布输入文字，点击其他位置完成' : '绘制完成后点击右下角，作为图片附件提问')}</div>
      <button class="sketch-done sketch-button" title="完成并添加图片" aria-label="完成并添加图片" disabled={busy() || (!shapes().length && !editing()?.text.trim())} onClick={() => void save()}>{busy() ? '…' : '✓'}</button>
    </div>
  </Portal>;
}
