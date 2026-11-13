import { For, Show, batch, createEffect, createMemo, createSignal, onCleanup, onMount } from 'solid-js';
import type { ModelParameter, ModelSection, VectorNode } from '../api/intelligentPage';
import type { CanvasStyle } from '../api/canvas';
import { evaluateGraphics, evaluateModel, modelDefaults, modelNumber, parameterValue } from '../utils/pageModel';
import { animationValues, createPlayback } from '../utils/pageAnimation';
import ModelDiagram from './ModelDiagram';
import { diagramPalette } from '../utils/modelDiagram';
import { saveGif } from '../utils/saveGif';

// Resource loading must remain cancellable before the encoder itself starts.
export function waitForGifResource<T>(resource: Promise<T>, signal: AbortSignal): Promise<T> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const finish = (error?: unknown, value?: T) => {
      clearTimeout(timer); signal.removeEventListener('abort', abort);
      error ? reject(error) : resolve(value!);
    };
    const abort = () => finish(signal.reason || new DOMException('已取消', 'AbortError'));
    const timer = setTimeout(() => finish(Error('GIF导出资源加载超时，请重新加载界面后重试')), 15000);
    signal.addEventListener('abort', abort, { once: true });
    resource.then(value => finish(undefined, value), error => finish(error));
  });
}

export default function PageModel(props: { section: ModelSection; active?: boolean; style?: CanvasStyle; trees?: Array<VectorNode | null>; onValues: (values: Record<string, number> | null) => void }) {
  let element!: HTMLDivElement;
  const [values, setValues] = createSignal(modelDefaults(props.section));
  const [error, setError] = createSignal('');
  const [playing, setPlaying] = createSignal(false);
  const [progress, setProgress] = createSignal(0);
  const [reducedMotion, setReducedMotion] = createSignal(false);
  const [exportProgress, setExportProgress] = createSignal<number | null>(null);
  const [exportNotice, setExportNotice] = createSignal('');
  const [reloadNeeded, setReloadNeeded] = createSignal(false);
  let exportAbort: AbortController | undefined, disposed = false;
  const animation = props.section.animation;
  const playback = animation ? createPlayback(animation.duration_seconds, (position, running) => batch(() => {
    setProgress(position); setPlaying(running);
    setValues(previous => animationValues(animation, position, previous));
  })) : undefined;
  const results = createMemo(() => evaluateModel(props.section, values()));
  const graphics = createMemo(() => evaluateGraphics(props.section, results()));
  const colorNames = { accent: '主题色', positive: '绿色', warning: '琥珀色', danger: '红色', muted: '灰色' };
  createEffect(() => props.onValues(values()));
  createEffect(() => { if (props.active === false) playback?.pause(); });
  onCleanup(() => { disposed = true; exportAbort?.abort(); playback?.dispose(); props.onValues(null); });
  const exportGif = async () => {
    if (exportAbort) return;
    playback?.pause();
    const controller = new AbortController(); exportAbort = controller;
    const section = props.section, inputs = { ...values() }, trees = props.trees ?? [], palette = diagramPalette(props.style ?? 'clay');
    setExportProgress(0); setExportNotice(''); setReloadNeeded(false);
    try {
      const { encodeModelGif } = await waitForGifResource(import('../utils/modelGif'), controller.signal);
      const blob = await encodeModelGif(section, inputs, trees, palette, controller.signal, value => { if (!disposed) setExportProgress(value); });
      controller.signal.throwIfAborted();
      if (disposed) return;
      setExportNotice('GIF编码完成，请在系统窗口选择保存位置。');
      const saved=await saveGif(blob,section.title);
      controller.signal.throwIfAborted();
      if (!disposed) setExportNotice(saved==='saved'?'GIF已保存；包含完整时间轴及说明摘要，不修改页面版本。':saved==='cancelled'?'已取消保存GIF，可重新导出。':'GIF已交给浏览器下载，请检查下载结果；不修改页面版本。');
    } catch (reason) {
      if (!disposed) {
        const message = reason instanceof Error ? reason.message : 'GIF导出失败，请重试';
        const missingModule = !controller.signal.aborted && /dynamically imported module|Importing a module script failed|Loading chunk|导出资源加载超时/i.test(message);
        setReloadNeeded(missingModule);
        setExportNotice(controller.signal.aborted ? '已取消GIF导出。' : missingModule
          ? '导出所需的界面资源未能加载，可能已更新。请重新加载界面后再导出；已保存版本保留，临时参数将重置。' : message);
      }
    } finally { exportAbort = undefined; if (!disposed) setExportProgress(null); }
  };
  onMount(() => {
    if (!playback) return;
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const motion = () => {
      const reduced = media.matches || document.documentElement.dataset.reduceMotion === 'true';
      setReducedMotion(reduced); if (reduced) playback.pause();
    };
    motion(); media.addEventListener('change', motion);
    const settings = new MutationObserver(motion);
    settings.observe(document.documentElement, { attributes: true, attributeFilter: ['data-reduce-motion'] });
    const visibility = () => { if (document.hidden) playback.pause(); };
    document.addEventListener('visibilitychange', visibility);
    const background = () => playback.pause();
    window.addEventListener('blur', background);
    const observer = new IntersectionObserver(entries => { if (!entries[entries.length - 1]?.isIntersecting) playback.pause(); });
    observer.observe(element);
    onCleanup(() => { media.removeEventListener('change', motion); settings.disconnect(); observer.disconnect(); document.removeEventListener('visibilitychange', visibility); window.removeEventListener('blur', background); });
  });
  const reset = () => { batch(() => { playback?.reset(); setValues(modelDefaults(props.section)); setError(''); }); };
  const update = (parameter: ModelParameter, input: HTMLInputElement) => {
    const raw = input.value;
    playback?.pause();
    const value = parameterValue(parameter, raw);
    if (value === null) {
      setError(`${parameter.label}请输入 ${modelNumber(parameter.min)} 至 ${modelNumber(parameter.max)} 之间的有限数值。`);
      input.value = String(values()[parameter.id]);
    } else {
      const track = animation?.tracks.find(t => t.parameter_id === parameter.id);
      if (track && playback) {
        const position = (value - track.start) / (track.end - track.start);
        playback.seek(position); input.value = String(values()[parameter.id]);
        setError(position < 0 || position > 1 ? `${parameter.label}已限制在动画起点与终点之间。` : '');
      } else { setError(''); setValues(previous => ({ ...previous, [parameter.id]: value })); }
    }
  };
  return <div class="page-model" ref={element}>
    <p>{props.section.description}</p>
    <Show when={animation && playback}><div class="page-model-playback">
      <div class="page-model-playback-buttons">
        <button type="button" disabled={reducedMotion() || props.active === false} onClick={() => playing() ? playback!.pause() : playback!.play()}>{playing() ? '暂停' : progress() >= 1 ? '重新播放' : '播放'}</button>
        <button type="button" onClick={reset}>重置动画</button>
        <button type="button" disabled={exportProgress() !== null || !props.section.graphics?.length} onClick={() => void exportGif()}>导出 GIF</button>
        <Show when={exportProgress() !== null}><button type="button" onClick={() => exportAbort?.abort()}>取消导出</button></Show>
        <span role="status">{playing() ? '正在播放' : progress() >= 1 ? '播放完成' : '已暂停'}</span>
      </div>
      <label>动画进度<input type="range" min="0" max="100" step="0.1" value={progress() * 100} aria-valuetext={`${(progress() * 100).toFixed(1)}%`} onInput={event => playback!.seek(Number(event.currentTarget.value) / 100)} /></label>
      <output>{(progress() * 100).toFixed(1)}% · {(progress() * animation!.duration_seconds).toFixed(1)} / {animation!.duration_seconds} 秒</output>
      <p>{reducedMotion() ? '已减少动态效果，可手动调整进度查看结果。' : '单次播放；调整进度或参数会暂停。收起、离屏或切到后台后暂停，返回时需手动继续。'}</p>
      <p>GIF从起点导出完整动画，固定当前风格与非动画参数；最多181帧、24MB，长动画会降低采样频率。仅导出图形和结果说明摘要。</p>
      <Show when={exportProgress() !== null}><label>GIF导出进度<progress max="1" value={exportProgress() ?? 0} />{Math.round((exportProgress() ?? 0) * 100)}%</label></Show>
      <Show when={exportNotice()}><p role="status">{exportNotice()}</p></Show>
      <Show when={reloadNeeded()}><button type="button" onClick={() => window.location.reload()}>重新加载界面</button></Show>
    </div></Show>
    <div class="page-model-controls"><For each={props.section.parameters}>{parameter => <fieldset>
      <legend>{parameter.label}{parameter.unit ? `（${parameter.unit}）` : ''}</legend>
      <input type="range" aria-label={`${parameter.label}滑块`} min={parameter.min} max={parameter.max} step={parameter.step} value={values()[parameter.id]} onInput={event => update(parameter, event.currentTarget)} />
      <label>数值<input type="number" min={parameter.min} max={parameter.max} step="any" value={values()[parameter.id]} onChange={event => update(parameter, event.currentTarget)} /></label>
      <small>{modelNumber(parameter.min)} — {modelNumber(parameter.max)}</small>
    </fieldset>}</For></div>
    <Show when={error()}><p role="alert">{error()}</p></Show>
    <Show when={graphics().length}>
      <figure class="page-model-diagram">
        <figcaption>参数联动示意图 <span>画面坐标 400 × 240，几何单位为示意单位</span></figcaption>
        <ModelDiagram section={props.section} results={results()} trees={props.trees} style={props.style} />
        <div class="page-model-graphic-legend" aria-live={playing() ? 'off' : 'polite'}><For each={graphics()}>{graphic => <p>
          <span class="page-model-swatch" style={{ background: graphic.available ? `var(--model-${graphic.color})` : 'transparent' }} aria-hidden="true" />
          <strong>{graphic.label}</strong>：{graphic.available ? `位置（${modelNumber(graphic.x)}，${modelNumber(graphic.y)}），宽 ${modelNumber(graphic.width)}、高 ${modelNumber(graphic.height)}；${colorNames[graphic.color]}${graphic.state ? ` · ${graphic.state}` : ''}` : '关联结果暂不可计算，图形暂不显示'}
          <For each={graphic.notes}>{note => <span>；{note}</span>}</For>
        </p>}</For></div>
      </figure>
    </Show>
    <div class="page-model-results" aria-live={playing() ? 'off' : 'polite'} aria-atomic="true"><For each={results()}>{result => <div class="page-model-result">
      <div class="page-model-value"><span>{result.output.label}</span><strong>{result.value === null ? '暂不可计算' : `${modelNumber(result.value)} ${result.output.unit}`}</strong></div>
      <Show when={result.value !== null} fallback={<p>{result.error}</p>}>
        <svg viewBox="0 0 400 40" role="img" aria-label={`${result.output.label}：${modelNumber(result.value!)} ${result.output.unit}，图形范围 ${modelNumber(result.output.min)} 至 ${modelNumber(result.output.max)}`}>
          <rect x="0" y="8" width="400" height="24" rx="12" fill="currentColor" opacity=".08" />
          <rect x="0" y="8" width={result.position * 4} height="24" rx="12" fill="var(--page-accent)" />
        </svg>
        <p class="page-model-scale">显示范围：{modelNumber(result.output.min)} — {modelNumber(result.output.max)} {result.output.unit}{result.outside ? '；结果超出图形范围，数值保留实际结果' : ''}</p>
        <Show when={result.state}>{state => <p class="page-model-state"><strong>{state().label}</strong><br />{state().description}</p>}</Show>
        <p>{result.output.description}</p>
        <details><summary>查看计算依据</summary><p>{result.equation} = {modelNumber(result.value!)} {result.output.unit}</p><Show when={result.output.threshold}>{threshold => <p>结果小于 {modelNumber(threshold().value)} 时为“{threshold().below.label}”，否则为“{threshold().at_or_above.label}”。</p>}</Show></details>
      </Show>
    </div>}</For></div>
    <div class="page-model-footer"><button type="button" onClick={reset}>恢复初始参数</button><span>临时试算，不生成内容版本；重新打开或切换版本会恢复保存值。试算结果不代表实验验证。</span></div>
  </div>;
}
