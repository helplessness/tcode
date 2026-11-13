import { For, Show, createEffect, createMemo, createSignal, on, onCleanup, onMount } from 'solid-js';
import type { CanvasAnimation, CanvasPart, CanvasStyle } from '../api/canvas';
import { createPlayback } from '../utils/pageAnimation';
import { CANVAS_GIF_WIDTH, CANVAS_GIF_HEIGHT, canvasExportPalette, canvasGifFrameSvg, requestCanvasMotionSnapshot, timelineShare } from '../utils/canvasAnimation';
import { saveGif } from '../utils/saveGif';
import { assemblyDirection, assemblyStepIndex, assemblySteps } from '../utils/assemblySteps';

export default function CanvasTimeline(props: {
  part: CanvasPart; style: CanvasStyle; revision: string; active: boolean; resetToken: number; canEdit: boolean;
  frame: () => HTMLIFrameElement | undefined; nonce: () => string;
  onFrame: (value: { id: string; share: number; focus?: string[] } | null) => void;
  onSave: (animation: CanvasAnimation) => void;
  onCancelled?: () => void;
  capture?: (animation:CanvasAnimation) => Promise<{render: (progress:number,signal:AbortSignal)=>Promise<Uint8ClampedArray>;dispose:()=>void}>;
}) {
  let element!: HTMLDivElement;
  const definition = props.part.animation || { duration_seconds: 6, mode: 'roundtrip' as const };
  const [progress, setProgress] = createSignal(0), [playing, setPlaying] = createSignal(false);
  const [reduced, setReduced] = createSignal(false), [notice, setNotice] = createSignal('');
  const [duration, setDuration] = createSignal(String(definition.duration_seconds));
  const [mode, setMode] = createSignal(definition.mode);
  const [started, setStarted] = createSignal(false);
  const steps = createMemo(() => assemblySteps(props.part.solid_scene, mode()));
  const count = () => steps().length;
  const stepIndex = createMemo(() => assemblyStepIndex(progress(), count()));
  const currentStep = () => steps()[stepIndex()];
  const [exportProgress, setExportProgress] = createSignal<number | null>(null);
  const [reloadNeeded, setReloadNeeded] = createSignal(false);
  let controller: AbortController | undefined, disposed = false;
  const publish = (value:number,running:boolean) => {
    setProgress(value); setPlaying(running); setStarted(true);
    props.onFrame({ id: props.part.element_id, share: timelineShare(value, mode(), !!count()),
      focus:currentStep()?.plans.map(plan=>plan.id) });
  };
  let playback = createPlayback(definition.duration_seconds,publish);
  const validDuration=()=>Number.isFinite(Number(duration()))&&Number(duration())>=1&&Number(duration())<=60;
  const dirty=()=>Number(duration())!==definition.duration_seconds||mode()!==definition.mode;
  const preview=()=>{if(validDuration()){playback.dispose();playback=createPlayback(Number(duration()),publish);playback.reset();setNotice('');}};
  createEffect(() => { if (!props.active && playing()) playback.pause(); });
  createEffect(on(() => props.resetToken, () => { playback.dispose(); playback=createPlayback(validDuration()?Number(duration()):definition.duration_seconds,publish); setPlaying(false); setProgress(0); setStarted(false); controller?.abort(); }, { defer: true }));
  createEffect(on(() => [props.revision, props.style], () => controller?.abort(), { defer: true }));
  onCleanup(() => { disposed = true; if(controller){controller.abort();props.onCancelled?.();} playback.dispose(); props.onFrame(null); });
  onMount(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const motion = () => { const value = media.matches || document.documentElement.dataset.reduceMotion === 'true'; setReduced(value); if (value && playing()) playback.pause(); };
    motion(); media.addEventListener('change', motion);
    const settings = new MutationObserver(motion); settings.observe(document.documentElement, { attributes: true, attributeFilter: ['data-reduce-motion'] });
    const visibility = () => { if (document.hidden && playing()) playback.pause(); };
    const blur = () => { if (playing()) playback.pause(); };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('blur', blur);
    const cancel=(event:KeyboardEvent)=>{if(event.key==='Escape'&&controller){event.preventDefault();controller.abort();}};window.addEventListener('keydown',cancel);
    const observer = new IntersectionObserver(entries => { if (!entries[entries.length-1]?.isIntersecting && playing()) playback.pause(); }); observer.observe(element.closest('.intelligent-canvas') || element);
    onCleanup(() => { media.removeEventListener('change', motion); settings.disconnect(); observer.disconnect(); document.removeEventListener('visibilitychange', visibility); window.removeEventListener('blur', blur);window.removeEventListener('keydown',cancel); });
  });
  const exportGif = async () => {
    if (controller) return;
    playback.pause();
    const abort = new AbortController(); controller = abort; setExportProgress(0); setNotice(''); setReloadNeeded(false);
    const palette = canvasExportPalette, animation = {duration_seconds:Number(duration()),mode:mode()}, title = props.part.label;
    let completed=0;
    let capture: Awaited<ReturnType<NonNullable<typeof props.capture>>> | undefined;
    try {
      // Allow the parent highlight effect to prepare the same measured motion plan.
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      abort.signal.throwIfAborted();
      capture = props.capture ? await props.capture(animation) : undefined;
      const snapshot = capture ? undefined : await requestCanvasMotionSnapshot(props.frame(), props.nonce(), props.part.element_id, abort.signal);
      const { encodeSvgGif } = await import('../utils/modelGif');
      const blob = await encodeSvgGif(animation.duration_seconds, value => snapshot ? canvasGifFrameSvg(snapshot, animation, value, title, palette) : String(value), abort.signal, value => { completed=value;if (!disposed) setExportProgress(value); }, capture ? (_svg,signal)=>capture!.render(Number(_svg),signal) : undefined,CANVAS_GIF_WIDTH,CANVAS_GIF_HEIGHT);
      abort.signal.throwIfAborted(); if (disposed) return;
      capture?.dispose(); capture=undefined;
      setNotice('GIF编码完成，请在系统窗口选择保存位置。');
      const saved=await saveGif(blob,title);
      abort.signal.throwIfAborted();
      if (!disposed) setNotice(saved==='saved'?'GIF已保存；包含完整时间轴，不增加内容版本。':saved==='cancelled'?'已取消保存GIF，可重新导出。':'GIF已交给浏览器下载，请检查下载结果；不增加内容版本。');
    } catch (error) {
      if (!disposed) {
        const message=error instanceof Error?error.message:'GIF导出失败，请重试';
        const missingModule=!abort.signal.aborted&&/dynamically imported module|Importing a module script failed|Loading chunk/i.test(message);
        setReloadNeeded(missingModule);
        setNotice(abort.signal.aborted ? `已取消GIF导出 · 编码进度${Math.round(completed*100)}%。` : missingModule ? '导出资源已更新或未能加载。请重新加载界面后再导出；已保存版本保留，临时查看状态会重置。' : message);
      }
    } finally { capture?.dispose(); controller = undefined; if (!disposed) setExportProgress(null); }
  };
  const save = () => {
    const seconds = Number(duration());
    if (!duration().trim() || !Number.isFinite(seconds) || seconds < 1 || seconds > 60) { setNotice('动画时长请输入1至60秒。'); return; }
    props.onSave({ duration_seconds: seconds, mode: mode() });
  };
  return <div class="intelligent-canvas-timeline" ref={element} role="group" aria-label="拆解动画时间轴">
    <div class="intelligent-canvas-timeline-toolbar">
      <strong>{count()?'逐步装配':'拆解动画'}</strong>
      <label>时长<input aria-label="动画时长（秒）" type="number" min="1" max="60" step="0.5" value={duration()} disabled={exportProgress()!==null} onInput={event=>{setDuration(event.currentTarget.value);preview();}} />秒</label>
      <label>模式<select aria-label="播放模式" value={mode()} disabled={exportProgress()!==null} onChange={event=>{setMode(event.currentTarget.value as CanvasAnimation['mode']);preview();}}><option value="roundtrip">拆解后合拢</option><option value="explode">只拆解</option><option value="assemble">只合拢</option></select></label>
      <button type="button" title="保存设置会创建内容版本，并随研究包交接" disabled={!props.canEdit || !dirty() || !validDuration() || exportProgress()!==null} onClick={save}>保存动画</button>
      <button type="button" disabled={!props.active || !validDuration() || exportProgress()!==null} onClick={()=>void exportGif()}>导出 GIF</button>
    </div>
    <Show when={count()}>
      <div class="canvas-assembly-steps" role="group" aria-label="装配步骤">
        <button type="button" disabled={!props.active || progress()<=0 || exportProgress()!==null}
          onClick={()=>playback.seek((Math.ceil(progress()*count()-1e-8)-1)/count())}>上一步</button>
        <button type="button" disabled={!props.active || progress()>=1 || exportProgress()!==null}
          onClick={()=>playback.seek(started()?(Math.floor(progress()*count()+1e-8)+1)/count():0)}>{started()?'下一步':'查看第一步'}</button>
        <div role="status">
          <Show when={started()} fallback={<p>装配总览 · 查看第一步，或直接拖动进度</p>}>
            <strong>第 {stepIndex()+1} / {count()} 步 · {currentStep()?.sign===-1?'装配':'拆解'}{progress()>=1?' · 已到终点':progress()>0&&Math.abs(progress()*count()-stepIndex()-1)<1e-8?' · 已完成':''}</strong>
            <For each={currentStep()?.plans}>{plan=><p>{plan.label} · {assemblyDirection(plan.explode!,currentStep()!.sign)}{plan.description?` · ${plan.description}`:''}</p>}</For>
          </Show>
        </div>
      </div>
    </Show>
    <div class="intelligent-canvas-timeline-playback">
      <button class="canvas-play" type="button" aria-label={playing()?'暂停动画':'播放动画'} disabled={!props.active || reduced() || !validDuration() || exportProgress() !== null} onClick={() => playing() ? playback.pause() : playback.play()}>{playing() ? '暂停' : progress() >= 1 ? '重播' : '播放'}</button>
      <button type="button" aria-label="重置动画" disabled={exportProgress() !== null} onClick={() => playback.reset()}>重置</button>
      <label>进度<input aria-label="动画进度" type="range" min="0" max="1" step="any" value={progress()} disabled={!props.active || exportProgress() !== null} aria-valuetext={`${(progress()*100).toFixed(1)}%`} onInput={event => playback.seek(Number(event.currentTarget.value))} /></label>
      <output>{(progress()*Number(duration())).toFixed(1)} / {duration()} 秒</output>
    </div>
    <Show when={exportProgress()!==null}><button type="button" onClick={()=>controller?.abort()}>取消导出 · Esc</button></Show>
    <Show when={reduced() || dirty()}><small role="status">{reduced()?'已减少动态效果，可拖动进度。':'未保存 · 当前设置已用于预览'}</small></Show>
    <Show when={exportProgress() !== null}><label>GIF导出进度<progress max="1" value={exportProgress() ?? 0} />{Math.round((exportProgress() ?? 0)*100)}%</label></Show>
    <Show when={notice()}><p role="status">{notice()}</p></Show>
    <Show when={reloadNeeded()}><button type="button" onClick={()=>window.location.reload()}>重新加载界面</button></Show>
  </div>;
}
