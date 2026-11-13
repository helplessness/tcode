import DOMPurify from 'dompurify';
import type { CanvasAnimation } from '../api/canvas';
import { xml, type DiagramPalette } from './modelDiagram';

export const canvasExportPalette: DiagramPalette = {background:'#f6f7f4',text:'#26332e',mutedText:'#68776f',accent:'#718b76',positive:'#46775b',warning:'#9b763c',danger:'#a4564c',muted:'#8b9690'};
export const CANVAS_GIF_WIDTH=960, CANVAS_GIF_HEIGHT=720;

// This exact function is also embedded into the fixed sandbox bridge.
export function explosionPortion(value: number, delay: number, last: number) {
  const step = Math.max(0, Math.min(1, value * (1 + last) - delay));
  return last ? step * step * (3 - 2 * step) : step;
}
export function timelineShare(progress: number, mode: CanvasAnimation['mode'], linear = false) {
  const p = Math.max(0, Math.min(1, progress));
  const stage = mode === 'roundtrip' ? (p <= .5 ? p * 2 : (1 - p) * 2) : mode === 'assemble' ? 1 - p : p;
  return linear ? stage : stage * stage * (3 - 2 * stage);
}
export type CanvasMotionSnapshot = {
  source: string;
  plans: Array<{ id: string; label: string; transform: string | null; x: number; y: number; delay: number }>;
};
export function requestCanvasMotionSnapshot(frame: HTMLIFrameElement | undefined, nonce: string, id: string, signal: AbortSignal): Promise<CanvasMotionSnapshot> {
  return new Promise((resolve, reject) => {
    if (!frame?.contentWindow) { reject(Error('图解尚未准备好，请稍后重试')); return; }
    signal.throwIfAborted();
    const requestId = crypto.randomUUID();
    const finish = (error?: unknown, snapshot?: CanvasMotionSnapshot) => {
      clearTimeout(timer); window.removeEventListener('message', receive); signal.removeEventListener('abort', abort);
      error ? reject(error) : resolve(snapshot!);
    };
    const abort = () => finish(signal.reason || Error('已取消'));
    const receive = (event: MessageEvent) => {
      if (event.source !== frame.contentWindow || event.data?.nonce !== nonce || event.data?.request_id !== requestId || event.data?.type !== 'tcode-canvas-animation-snapshot-result') return;
      const snapshot = event.data.snapshot as CanvasMotionSnapshot;
      if (event.data.error) { finish(Error(event.data.error)); return; }
      if (!snapshot || typeof snapshot.source !== 'string' || snapshot.source.length > 1_500_000 || !Array.isArray(snapshot.plans) || snapshot.plans.length < 2 || snapshot.plans.length > 60
        || snapshot.plans.some(p => !p || typeof p.id !== 'string' || typeof p.label !== 'string' || ![p.x,p.y,p.delay].every(Number.isFinite) || Math.max(Math.abs(p.x),Math.abs(p.y)) > 100000 || p.delay < 0 || p.delay > 2.75 || (p.transform !== null && (typeof p.transform !== 'string' || !/^translate\([0-9eE+.,\s-]+\)$/.test(p.transform))))) {
        finish(Error('拆解快照不完整，请重新加载图解')); return;
      }
      finish(undefined, snapshot);
    };
    const timer = setTimeout(() => finish(Error('读取拆解图形超时，请重新展开图解')), 5000);
    window.addEventListener('message', receive); signal.addEventListener('abort', abort, { once: true });
    frame.contentWindow.postMessage({ type: 'tcode-canvas-animation-snapshot', nonce, request_id: requestId, element_id: id }, '*');
  });
}
export function canvasGifFrameSvg(snapshot: CanvasMotionSnapshot, animation: CanvasAnimation, progress: number, title: string, palette: DiagramPalette) {
  const document = new DOMParser().parseFromString(DOMPurify.sanitize(snapshot.source, { USE_PROFILES: { svg: true }, FORBID_TAGS: ['script', 'foreignObject', 'style', 'a', 'image', 'use', 'animate', 'set'], FORBID_ATTR: ['href', 'xlink:href', 'style'] }), 'image/svg+xml');
  if (document.querySelector('parsererror') || document.documentElement.localName !== 'svg') throw Error('动画图形快照无效');
  const root = document.documentElement;
  const nodes = new Map([...root.querySelectorAll('[id]')].map(node => [node.id, node]));
  const share = timelineShare(progress, animation.mode), last = Math.max(0, ...snapshot.plans.map(p => p.delay));
  const summaries = snapshot.plans.map((plan, i) => {
    if (![plan.x, plan.y, plan.delay].every(Number.isFinite)) throw Error('拆解方向无效');
    const node = nodes.get(plan.id); if (!node) throw Error('拆解部件缺失');
    const portion = explosionPortion(share, plan.delay, last);
    if (portion) node.setAttribute('transform', `translate(${plan.x * portion} ${plan.y * portion})${plan.transform ? ` ${plan.transform}` : ''}`);
    else if (plan.transform === null) node.removeAttribute('transform'); else node.setAttribute('transform', plan.transform);
    return i >= 6 ? '' : `<text x="${32+(i%3)*300}" y="${624+Math.floor(i/3)*25}" font-size="14">${xml([...plan.label].slice(0, 16).join(''))} · ${plan.x === 0 && plan.y === 0 ? '保持固定' : `展开 ${Math.round(portion * 100)}%`}</text>`;
  });
  root.setAttribute('x', '24'); root.setAttribute('y', '76'); root.setAttribute('width', '912'); root.setAttribute('height', '520');
  const graphic = new XMLSerializer().serializeToString(root);
  return `<svg xmlns="http://www.w3.org/2000/svg" width="960" height="720" viewBox="0 0 960 720"><rect width="960" height="720" fill="${xml(palette.background)}"/><g font-family="system-ui,sans-serif" fill="${xml(palette.text)}"><text x="32" y="36" font-size="22">${xml([...title].slice(0, 36).join(''))}</text><text x="32" y="60" font-size="14">${(progress * 100).toFixed(1)}% · ${(progress * animation.duration_seconds).toFixed(1)} / ${animation.duration_seconds} 秒</text>${graphic}${summaries.join('')}<text x="32" y="696" font-size="12" fill="${xml(palette.mutedText)}">拆解示意 · ${snapshot.plans.length > 6 ? '更多部件说明保留在画布中' : '不代表真实机械装配验证'}</text></g></svg>`;
}
