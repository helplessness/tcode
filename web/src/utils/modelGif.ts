import { GIFEncoder, quantize, applyPalette } from 'gifenc';
import type { ModelSection, VectorNode } from '../api/intelligentPage';
import { animationValues } from './pageAnimation';
import { evaluateModel, modelNumber } from './pageModel';
import { modelDiagramMarkup, xml, type DiagramPalette } from './modelDiagram';

export const GIF_WIDTH = 400, GIF_HEIGHT = 600;
export function gifPlan(duration: number) {
  if (!Number.isFinite(duration) || duration < 1 || duration > 60) throw Error('动画时长须为1至60秒');
  const intervals = Math.min(180, Math.ceil(duration * 10));
  return Array.from({ length: intervals + 1 }, (_, i) => ({ progress: i / intervals, delay: i === intervals ? 10 : (Math.round((i + 1) * duration * 100 / intervals) - Math.round(i * duration * 100 / intervals)) * 10 }));
}
const excerpt = (value: string, max = 28) => { const chars = Array.from(value); return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : value; };
export function gifFrameSvg(section: ModelSection, progress: number, inputs: Record<string, number>, trees: Array<VectorNode | null>, palette: DiagramPalette) {
  if (!section.animation) throw Error('此区块没有动画定义');
  const values = animationValues(section.animation, progress, inputs), results = evaluateModel(section, values);
  const lines = results.map((r, i) => {
    const y = 326 + i * 58;
    const value = `${excerpt(r.output.label, 10)}：${r.value === null ? '暂不可计算' : modelNumber(r.value)} ${excerpt(r.output.unit, 6)}`;
    const description = r.error || `${r.state?.label ?? ''} ${r.state?.description || r.output.description}`;
    return `<text x="16" y="${y}" font-size="12">${xml(value)}</text><text x="16" y="${y + 20}" font-size="11" fill="${xml(palette.mutedText)}">${xml(excerpt(description))}</text>`;
  }).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${GIF_WIDTH}" height="${GIF_HEIGHT}" viewBox="0 0 ${GIF_WIDTH} ${GIF_HEIGHT}"><rect width="400" height="600" fill="${xml(palette.background)}"/><g font-family="system-ui,sans-serif" fill="${xml(palette.text)}"><text x="16" y="28" font-size="16">${xml(excerpt(section.title, 22))}</text><text x="16" y="48" font-size="12">${(progress * 100).toFixed(1)}% · ${(progress * section.animation.duration_seconds).toFixed(1)} / ${section.animation.duration_seconds} 秒</text><svg x="16" y="64" width="368" height="221" viewBox="0 0 400 240">${modelDiagramMarkup(section, results, trees, palette)}</svg>${lines}<text x="16" y="584" font-size="11" fill="${xml(palette.mutedText)}">示意动画 · 说明仅摘要，完整定义保留在页面中</text></g></svg>`;
}

export async function renderGifRgba(svg: string, signal: AbortSignal, width = GIF_WIDTH, height = GIF_HEIGHT): Promise<Uint8ClampedArray> {
  signal.throwIfAborted();
  const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
  const image = new Image();
  try {
    await new Promise<void>((resolve, reject) => {
      const abort = () => finish(signal.reason || new DOMException('已取消', 'AbortError'));
      const timer = setTimeout(() => finish(Error('图形帧渲染超时')), 10000);
      const finish = (error?: unknown) => {
        clearTimeout(timer); signal.removeEventListener('abort', abort); image.onload = image.onerror = null;
        error ? reject(error) : resolve();
      };
      signal.addEventListener('abort', abort, { once: true });
      image.onload = () => finish(); image.onerror = () => finish(Error('图形帧无法渲染'));
      image.src = url;
    });
    signal.throwIfAborted();
    const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    if (!context) throw Error('此环境无法创建图形导出画布');
    context.drawImage(image, 0, 0);
    return context.getImageData(0, 0, width, height).data;
  } finally { image.removeAttribute('src'); URL.revokeObjectURL(url); }
}

export async function encodeModelGif(section: ModelSection, inputs: Record<string, number>, trees: Array<VectorNode | null>, palette: DiagramPalette,
  signal: AbortSignal, onProgress: (fraction: number) => void, render = renderGifRgba) {
  if (!section.animation || !section.graphics?.length) throw Error('GIF导出需要动画定义和图形');
  return encodeSvgGif(section.animation.duration_seconds, progress => gifFrameSvg(section, progress, inputs, trees, palette), signal, onProgress, render);
}

export async function encodeSvgGif(duration: number, frameSvg: (progress: number) => string,
  signal: AbortSignal, onProgress: (fraction: number) => void, render = renderGifRgba, width = GIF_WIDTH, height = GIF_HEIGHT) {
  if (!Number.isInteger(width)||!Number.isInteger(height)||width<1||height<1||width*height>960*960) throw Error('GIF尺寸超出限制');
  signal.throwIfAborted();
  const worker = typeof Worker !== 'undefined' ? new Worker(new URL('./gifWorker.ts',import.meta.url),{type:'module'}) : undefined;
  const request = (payload: unknown, transfer: Transferable[] = []) => new Promise<any>((resolve,reject)=>{
    signal.throwIfAborted();
    const finish=(error?:unknown,result?:unknown)=>{clearTimeout(timer);worker!.removeEventListener('message',message);worker!.removeEventListener('error',failed);signal.removeEventListener('abort',abort);error?reject(error):resolve(result);};
    const message=(event:MessageEvent)=>finish(event.data.error?Error(event.data.error):undefined,event.data);
    const failed=()=>finish(Error('GIF编码器无法运行，请重试'));
    const abort=()=>{worker!.terminate();finish(signal.reason||Error('已取消'));};
    const timer=setTimeout(()=>finish(Error('GIF单帧编码超时')),10000);
    worker!.addEventListener('message',message);worker!.addEventListener('error',failed);signal.addEventListener('abort',abort,{once:true});worker!.postMessage(payload,transfer);
  });
  try {
  const plan = gifPlan(duration), encoder = GIFEncoder();
  if(worker)await request({type:'init',width,height});
  let bytes = 0;
  for (let i = 0; i < plan.length; i++) {
    signal.throwIfAborted();
    const frame = plan[i];
    const rgba = await render(frameSvg(frame.progress), signal, width, height);
    signal.throwIfAborted();
    if (rgba.length !== width * height * 4) throw Error('导出帧尺寸不一致');
    if (worker) {
      const buffer = rgba.buffer.slice(rgba.byteOffset,rgba.byteOffset+rgba.byteLength) as ArrayBuffer;
      await request({type:'frame',rgba:buffer,delay:frame.delay},[buffer]);
    } else {
    const colors = quantize(rgba, 256), indexed = applyPalette(rgba, colors);
    encoder.writeFrame(indexed, width, height, { palette: colors, delay: frame.delay, repeat: -1, dispose: 2 });
    bytes = encoder.bytesView().length;
    if (bytes > 24 * 1024 * 1024) throw Error('GIF超过24MB，请缩短动画或简化图形');
    }
    onProgress((i + 1) / plan.length);
    await new Promise<void>(resolve => setTimeout(resolve, 0));
  }
  signal.throwIfAborted();
  if(worker){const result=await request({type:'finish'});signal.throwIfAborted();return new Blob([result.bytes],{type:'image/gif'});}
  encoder.finish();
  return new Blob([new Uint8Array(encoder.bytes())], { type: 'image/gif' });
  } finally { worker?.terminate(); }
}
