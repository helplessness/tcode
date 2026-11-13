import { For, Show, lazy, untrack, createEffect, createMemo, createResource, createSignal, createUniqueId, on, onCleanup, onMount } from 'solid-js';
import { canvasRequest, type CanvasData, type CanvasHistory, type CanvasOperation, type SpatialReport } from '../api/canvas';
import { composerDrafts, composerKey } from '../stores/composer';
import { sessionStore } from '../stores/session';
import { canvasFrame } from '../utils/canvasFrame';
import { passVerticalScrollToParent } from '../utils/scrollBoundary';
import { canvasSelectionRequest } from '../utils/canvasSelection';
import { canvasPosition, canvasTranslation } from '../utils/canvasPosition';
import { canvasHierarchy } from '../utils/canvasHierarchy';
import type { CanvasContentBlock } from '../utils/contentBlocks';
import './CanvasBlock.css';
import CanvasTimeline from './CanvasTimeline';
import RevisionDeletion from './RevisionDeletion';
import type {SolidView} from '../utils/solidScene';
const SolidSceneViewer=lazy(()=>import('./SolidSceneViewer'));


export default function CanvasBlock(props: { block: CanvasContentBlock; alternatives?: CanvasContentBlock[]; onAlternative?: (id:string)=>void; allowSessionActions?: boolean; onSaveHandoff?: (value: CanvasData) => Promise<void> }) {
  const [solidMode,setSolidMode]=createSignal(false);
  const [solidView,setSolidView]=createSignal<SolidView>();
  const [spatial,setSpatial]=createSignal<SpatialReport>();
  const [checking,setChecking]=createSignal(false);
  const [solidShare,setSolidShare]=createSignal(0);
  const [solidTarget,setSolidTarget]=createSignal('');
  let solidRaf=0;
  const [collapsed, setCollapsed] = createSignal(false);
  const [zoom, setZoom] = createSignal(1);
  const contentId = createUniqueId();
  const toolsId = createUniqueId();
  const toolsAnchor = `--canvas-tools-${toolsId}`;
  const [toolsOpen, setToolsOpen] = createSignal(false);
  const [revision, setRevision] = createSignal('');
  const [selected, setSelected] = createSignal('');
  const [focus, setFocus] = createSignal('');
  const [expandedComponent,setExpandedComponent]=createSignal('');
  const [appliedComponent,setAppliedComponent]=createSignal('');
  const [transitionPhase,setTransitionPhase]=createSignal('');
  const [timeline, setTimeline] = createSignal<{id:string;share:number;focus?:string[]}|null>(null);
  const [timelineReset, setTimelineReset] = createSignal(0);
  const stopTimeline = () => { setTimeline(null); setTimelineReset(value=>value+1); };
  const [mode, setMode] = createSignal<'explore' | 'edit'>('explore');
  const [color, setColor] = createSignal('#2563eb');
  const [colorTarget, setColorTarget] = createSignal<'fill' | 'stroke'>('fill');
  const [text, setText] = createSignal('');
  const [description, setDescription] = createSignal('');
  const [label, setLabel] = createSignal('');
  const [offsetX, setOffsetX] = createSignal('0');
  const [offsetY, setOffsetY] = createSignal('0');
  const [solidOffset, setSolidOffset] = createSignal(['0','0','0']);
  const solidTranslation = () => solidOffset().map(Number);
  const validSolidTranslation = () => solidOffset().every(v=>v.trim() && Number.isFinite(Number(v)) && Math.abs(Number(v))<=200) && solidTranslation().some(v=>v!==0);
  const [instruction, setInstruction] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal('');
  const [notice, setNotice] = createSignal('');
  const [historyBefore, setHistoryBefore] = createSignal('');
  const [researchId, setResearchId] = createSignal('');
  const [research, { refetch: refetchResearch }] = createResource(
    () => JSON.stringify([props.block.canvas_id, props.block.session_id, researchId()]),
    () => canvasRequest<{ enabled: boolean; projects: Array<{ research_id: string; title: string }>; record: { revision: number; artifact_refs: Array<{ revision_id: string }> } | null }>(
      props.block.canvas_id, props.block.session_id, '/research', undefined, '', '', researchId()),
  );
  // Refresh topic choices in place; reading research() during refresh would
  // suspend the entire diagram and collapse the conversation's scroll range.
  let frame: HTMLIFrameElement | undefined;
  let toolsMenu: HTMLDivElement | undefined;
  const [data, { mutate, refetch }] = createResource(
    () => JSON.stringify([props.block.canvas_id, props.block.session_id,revision() === 'latest' ? '' : revision() || props.block.revision_id]),
    key => {const [id,session,revision]=JSON.parse(key);return canvasRequest<CanvasData>(id, session, '', undefined, revision);},
  );
  const value = () => data.error ? undefined : data.latest;
  const [history, { refetch: refetchHistory }] = createResource(
    () => value() && JSON.stringify([props.block.canvas_id, props.block.session_id,value()?.current_revision_id,historyBefore()]),
    key => {const [id,session,,before]=JSON.parse(key);return canvasRequest<CanvasHistory>(id, session, '/revisions', undefined, '', before);},
  );
  createEffect(()=>{if(value()?.is_deleted&&props.allowSessionActions!==false)setRevision('latest');});
  createEffect(()=>{if(collapsed())toolsMenu?.hidePopover();});
  const style = () => value()?.display_style || 'clay';
  const hierarchy=createMemo(()=>canvasHierarchy(value()?.parts||[],value()?.elements||[]));
  const visibleParts=()=>mode()==='explore'?hierarchy().navigation(focus()||timelinePart()?.element_id||''):value()?.parts||[];
  const whole=()=>{setSpatial(undefined);stopTimeline();setExpandedComponent('');setAppliedComponent('');setTransitionPhase('');setFocus('');setSelected('');setZoom(1);};
  createEffect(on(()=>value()?.revision_id,whole));
  createEffect(()=>{if(mode()==='edit'){stopTimeline();setExpandedComponent('');setFocus('');}});
  const assembly=()=>hierarchy().assembly(focus())||timelinePart()?.element_id||'';
  const expansionCount=()=>hierarchy().children(assembly()).length;
  const timelinePart=createMemo(()=>value()?.parts.find(part=>part.illustration==='isometric'
    && hierarchy().children(part.element_id).length>=2 && hierarchy().children(part.element_id).length<=12
    && hierarchy().children(part.element_id).every(child=>child.explode)));
  createEffect(()=>{
    const expanded=expandedComponent();
    if(expanded&&!hierarchy().trail(focus()).some(part=>part.element_id===expanded))setExpandedComponent('');
  });
  const solidScene=()=>value()?.parts.find(p=>p.solid_scene)?.solid_scene;
  const solidExpansionReady=()=>!solidScene()||assembly()===solidScene()?.id||!!solidScene()?.assemblies.find(g=>g.id===assembly())?.pieces.every(p=>p.explode);
  createEffect(()=>{if(!solidScene())setSolidMode(false);});
  createEffect(()=>{
    const state=timeline(),expanded=expandedComponent(),scene=solidScene();
    if(!scene)return;
    cancelAnimationFrame(solidRaf);
    if(state){setSolidTarget(state.id);setSolidShare(state.share);setTransitionPhase('');return;}
    if(collapsed())return;
    const start=untrack(solidShare),end=expanded?1:0,target=expanded||untrack(solidTarget)||scene.id;
    setSolidTarget(target);
    setTransitionPhase(start===end?'':end?'opening':'closing');
    if(matchMedia('(prefers-reduced-motion: reduce)').matches||document.documentElement.dataset.reduceMotion==='true'){setSolidShare(end);setAppliedComponent(expanded);setTransitionPhase('');return;}
    const time=performance.now();
    const tick=(now:number)=>{const p=Math.min(1,(now-time)/500),ease=p*p*(3-2*p);setSolidShare(start+(end-start)*ease);if(p<1)solidRaf=requestAnimationFrame(tick);else{setAppliedComponent(expanded);setTransitionPhase('');}};
    solidRaf=requestAnimationFrame(tick);
  });
  onCleanup(()=>cancelAnimationFrame(solidRaf));
  const checkSpatial=async()=>{
    const current=value();if(!current||checking())return;
    setChecking(true);setError('');
    try{const result=await canvasRequest<SpatialReport>(props.block.canvas_id,props.block.session_id,'/spatial-check?suggest=true',undefined,current.revision_id);if(value()?.revision_id===current.revision_id)setSpatial(result);}
    catch(reason){setError(reason instanceof Error?reason.message:'拆解路径检查失败');}finally{setChecking(false);}
  };
  const drawing = createMemo(() => {
    const current = value();
    if (!current || solidScene()) return undefined;
    const nonce = crypto.randomUUID();
    return { nonce, html: canvasFrame(current.source, current.parts.map(p => p.element_id), nonce, style(),
      Object.fromEntries(current.parts.map(p => [p.element_id, p.label])),
      Object.fromEntries(current.parts.filter(p=>p.explode).map(p=>[p.element_id,p.explode!])),
      current.parts.some(p=>p.illustration==='isometric')) };
  });
  const part = createMemo(() => value()?.parts.find(p => p.element_id === selected()));
  const element = createMemo(() => value()?.elements.find(p => p.element_id === selected()));
  const position = createMemo(() => element() ? canvasPosition(element()?.transform) : null);
  const translation = createMemo(() => canvasTranslation(offsetX(), offsetY()));
  const canEdit = () => props.allowSessionActions !== false && !busy()
    && sessionStore.currentSessionId() === props.block.session_id
    && value()?.revision_id === value()?.current_revision_id;
  const highlight = () => frame?.contentWindow?.postMessage({
    type: 'tcode-canvas-highlight', nonce: drawing()?.nonce, element_id: selected(), focus_id:focus(), expanded_id:solidScene()?'':expandedComponent(),
    active:!collapsed()&&!solidScene(), reduced_motion:document.documentElement.dataset.reduceMotion==='true',
    timeline:solidScene()?null:timeline(),
  }, '*');
  const updateViewport = () => frame?.contentWindow?.postMessage({
    type: 'tcode-canvas-viewport', nonce: drawing()?.nonce, zoom: zoom(),
  }, '*');
  createEffect(() => { drawing(); zoom(); updateViewport(); });
  const choose = (id: string, exact=false) => {
    stopTimeline();
    if (!id) { setSelected(''); return; }
    if (!value()?.elements.some(e => e.element_id === id)) return;
    if(mode()==='explore') {
      id=exact?id:hierarchy().target(focus()||timelinePart()?.element_id||'',id);
      if(value()?.parts.some(p=>p.element_id===id))setFocus(id);
    }
    setSelected(id);
    setNotice('');
  };
  createEffect(() => { selected(); focus(); expandedComponent(); collapsed(); drawing(); timeline(); solidMode(); highlight(); });
  createEffect(()=>{
    if(collapsed())return;
    const task=requestAnimationFrame(()=>{updateViewport();highlight();});
    onCleanup(()=>cancelAnimationFrame(task));
  });
  onMount(() => {
    const receive = (event: MessageEvent) => {
      if (event.source !== frame?.contentWindow || !event.data || event.data.nonce !== drawing()?.nonce) return;
      if(event.data.type==='tcode-canvas-explode-result'){
        if(solidScene())return;
        if(event.data.requested_id!==expandedComponent())return;
        setTransitionPhase(event.data.phase==='opening'||event.data.phase==='closing'?event.data.phase:'');
        setAppliedComponent(event.data.expanded_id===expandedComponent()?expandedComponent():'');
        if(event.data.error){setExpandedComponent('');setNotice(event.data.error==='incomplete_component_plan'?'此组件的拆解方向与顺序不完整，请补齐所有直属零件的方案。':'此组件暂时无法展开：请确认有 2–12 个可测量的直属零件。');}
        return;
      }
      if(event.data.type !== 'tcode-canvas-select'||typeof event.data.element_id !== 'string')return;
      choose(event.data.element_id);
    };
    window.addEventListener('message', receive);
    onCleanup(() => window.removeEventListener('message', receive));
    const settingsChanged = () => { void refetch(); };
    window.addEventListener('tcode:settings-changed', settingsChanged);
    onCleanup(() => window.removeEventListener('tcode:settings-changed', settingsChanged));
  });
  createEffect(() => {
    const current = value();
    setSolidOffset(['0','0','0']);
    if (current && selected() && !current.elements.some(e => e.element_id === selected())) setSelected('');
    setText(current?.elements.find(e => e.element_id === selected())?.text || '');
    setDescription(current?.parts.find(p => p.element_id === selected())?.description || '');
    setLabel(current?.parts.find(p => p.element_id === selected())?.label || '');
    const offset = canvasPosition(current?.elements.find(e => e.element_id === selected())?.transform);
    setOffsetX(String(offset?.[0] || 0)); setOffsetY(String(offset?.[1] || 0));
  });

  const commit = async (suffix: '/edits' | '/restore', payload: unknown) => {
    const current = value();
    if (!current || busy() || props.allowSessionActions === false
      || sessionStore.currentSessionId() !== props.block.session_id
      || (suffix === '/edits' && !canEdit())) return;
    const canvasId = props.block.canvas_id;
    const sessionId = props.block.session_id;
    setBusy(true); setError(''); setNotice('');
    try {
      await canvasRequest(canvasId, sessionId, suffix, payload);
      const saved = await canvasRequest<CanvasData>(canvasId, sessionId);
      if (canvasId === props.block.canvas_id && sessionId === props.block.session_id) {
        setRevision(saved.revision_id); mutate(saved);
        setHistoryBefore('');
        setNotice('已保存新版本');
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败，原版本已保留。');
    } finally { setBusy(false); }
  };
  const edit = (operation: CanvasOperation) => void commit('/edits', {
    base_revision_id: value()?.revision_id, request_id: crypto.randomUUID(), element_ids: [selected()], operations: [operation],
  });
  const askAgent = async () => {
    const current = value();
    if (!current || !selected() || !instruction().trim() || !canEdit()) return;
    const canvasId = current.canvas_id, sessionId = props.block.session_id;
    const target = selected(), input = instruction();
    setBusy(true); setError('');
    try {
    const selection = await canvasRequest<{ selection_id: string }>(canvasId, sessionId, '/selection', {
      base_revision_id: current.revision_id, element_ids: [target],
    });
    if (sessionStore.currentSessionId() !== sessionId || props.block.canvas_id !== canvasId) return;
    const key = composerKey(sessionId);
    const existing = composerDrafts.get(key);
    const request = canvasSelectionRequest(canvasId, current.revision_id, target, input, selection.selection_id);
    composerDrafts.update(key, { text: [existing.text, request].filter(Boolean).join('\n\n') });
    setNotice('选区和要求已加入当前会话输入框，发送后由 Agent 处理。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '选区保存失败'); }
    finally { setBusy(false); }
  };

  const partIndex = () => value()?.parts.findIndex(p => p.element_id === selected()) ?? -1;
  const step = (offset: number) => {
    const parts = visibleParts();
    const next = parts[parts.findIndex(p=>p.element_id===selected()) + offset];
    if (next) choose(next.element_id,true);
  };

  return <section class="intelligent-canvas" data-style={style()} data-mode={mode()} data-collapsed={collapsed()} aria-label={`交互画布：${props.block.title}`}>
    <header class="intelligent-canvas-header">
      <div><span class="intelligent-canvas-eyebrow">交互图解</span><h3>{props.block.title}</h3></div>
        <button type="button" aria-expanded={!collapsed()} aria-controls={contentId} onClick={() => setCollapsed(value => !value)}>{collapsed() ? '展开图解' : '收起图解'}</button>
    </header>
    <div class="intelligent-canvas-content" id={contentId} hidden={collapsed()}>
    <Show when={data.loading}><p class="intelligent-canvas-message" role="status">正在加载画布…</p></Show>
    <Show when={data.error}><div class="intelligent-canvas-message" role="alert"><p>{data.error instanceof Error?data.error.message:"暂时无法读取画布，请重新加载；已保存数据不会因此删除。"}</p><button type="button" disabled={data.loading} onClick={()=>void refetch()}>重新加载画布</button></div></Show>
    <Show when={value()}>{current => <>
      <div class="intelligent-canvas-commandbar">        <Show when={(props.alternatives?.length||0)>1}><label>生成结果 <select aria-label="同名图解生成结果" value={props.block.canvas_id} onChange={event=>props.onAlternative?.(event.currentTarget.value)}>
          <For each={props.alternatives}>{(block,index)=><option value={block.canvas_id}>{index()===(props.alternatives?.length||0)-1?'最后生成':'较早生成'} · 第{index()+1}份</option>}</For>
        </select></label></Show>

        <div class="intelligent-canvas-control-group"><span>操作</span><div class="intelligent-canvas-modes" aria-label="画布模式">
          <button type="button" aria-pressed={mode() === 'explore'} onClick={() => setMode('explore')}>探索</button>
          <button type="button" aria-pressed={mode() === 'edit'} disabled={props.allowSessionActions === false} onClick={() => setMode('edit')}>编辑</button>
        </div></div>
        <Show when={solidScene()}><div class="intelligent-canvas-control-group"><span>视图</span><div class="intelligent-canvas-modes" aria-label="图形视图"><button type="button" aria-pressed={!solidMode()} onClick={()=>setSolidMode(false)}>图解</button><button type="button" aria-pressed={solidMode()} onClick={()=>setSolidMode(true)}>三维</button></div></div></Show>
        <Show when={value() && history.latest && !history.error}><label class="intelligent-canvas-version">版本<select aria-label="版本记录" value={value()?.revision_id} disabled={busy()||history.loading} onChange={e=>setRevision(e.currentTarget.value)}>
          <Show when={!history.latest?.items.some(item=>item.revision_id===value()?.revision_id)}><option value={value()?.revision_id}>正在查看的版本</option></Show>
          <For each={history.latest?.items}>{item=><option value={item.revision_id}>{item.revision_id===history.latest?.current_revision_id?'当前 · ':''}{new Date(item.created).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})} · {item.actor==='agent'?'生成':'修改'}</option>}</For>
        </select></label></Show>

        <Show when={value()?.revision_id!==value()?.current_revision_id}><button type="button" disabled={busy()} onClick={() => { setRevision('latest'); void refetch(); }}>回到最新</button></Show>

      <div class="intelligent-canvas-more"><button type="button" popovertarget={toolsId} aria-expanded={toolsOpen()} aria-controls={toolsId} style={`anchor-name:${toolsAnchor}`}>更多工具</button><div ref={toolsMenu} id={toolsId} popover="auto" class="intelligent-canvas-secondary" style={`position-anchor:${toolsAnchor}`} on:wheel={passVerticalScrollToParent} onToggle={event=>setToolsOpen(event.currentTarget.matches(':popover-open'))}>        <a href={`/api/intelligent-ui/v1/canvases/${encodeURIComponent(props.block.canvas_id)}/export?${new URLSearchParams({ session_id: props.block.session_id, revision_id: value()?.revision_id || props.block.revision_id })}`} download="canvas.svg">{solidScene()?'导出结构 SVG（静态参考）':'导出 SVG'}</a>
          <Show when={solidScene()}><details class="canvas-spatial intelligent-canvas-aux"><summary>装配与拆解路径检查{spatial()?` · ${spatial()!.initial_count+spatial()!.motion_count}项待核对`:''}</summary>
            <button type="button" disabled={checking()} onClick={()=>void checkSpatial()}>{checking()?'正在检查…':'检查并建议方向'}</button>
            <Show when={spatial()}>{report=><><p role="status">{report().status==='no_risk_detected'?'已检查项目未发现风险。':`需要核对：装配状态 ${report().initial_count} 项、拆解路径 ${report().motion_count} 项可能相交。轴孔配合见下方。`}</p>
              <div class="canvas-spatial-list"><For each={report().bore_fits}>{fit=><div>
                <strong>{fit.labels.join(' ↔ ')}</strong>
                <p>共轴：{fit.coaxial?'通过':'需核对，轴向或中心不一致'}</p>
                <p>孔径：{fit.diameter_ok===null?'未核实（无有效穿入段）':fit.diameter_ok?'通过':'需核对，穿入段过粗'} · 孔直径 {fit.bore_diameter.toFixed(2)}{fit.engaged_diameter===null?'':`，穿入段最大直径 ${fit.engaged_diameter.toFixed(2)}`}</p>
                <p>轴肩穿孔：{fit.shoulder_ok===null?'未核实':fit.shoulder_ok?'未发现超径':'需核对，路径中有超径轴段扫过孔位'}{fit.swept_diameter===null?'':` · 扫过孔位最大直径 ${fit.swept_diameter.toFixed(2)}`}</p>
              </div>}</For><For each={report().issues}>{issue=><button type="button" onClick={()=>choose(issue.element_ids[0],true)}>{issue.message}</button>}</For>
              <For each={report().suggestions}>{suggestion=><div><p>{suggestion.message} · 预计剩余{suggestion.remaining_motion_risks}项路径风险</p><button type="button" disabled={!canEdit()} onClick={()=>void commit('/edits',{base_revision_id:current().revision_id,request_id:crypto.randomUUID(),element_ids:[suggestion.element_id],operations:[{op:'set_solid_direction',element_id:suggestion.element_id,vector:suggestion.vector,order:suggestion.order}]})}>应用方向建议</button></div>}</For></div><small>{report().note}</small></>}</Show>
          </details></Show>
          <details class="intelligent-canvas-aux intelligent-canvas-quality">
            <summary>布局检查 · {current().layout_quality ? `${current().layout_quality!.counts.error} 项错误／${current().layout_quality!.counts.warning} 项提醒` : '未提供报告'}</summary>
            <p>静态检查仅覆盖部分布局，不能证明显示质量或科研结论正确。</p>
            <Show when={current().layout_quality}>{quality => <>
              <For each={quality().issues}>{issue => <div class="intelligent-canvas-quality-issue">
                <p>{issue.severity === 'error' ? '错误' : '提醒'}：{issue.message}</p>
                <For each={issue.element_ids.filter((id): id is string => Boolean(id && current().elements.some(e => e.element_id === id)))}>{id =>
                  <button type="button" onClick={() => choose(id)}>定位：{current().parts.find(p => p.element_id === id)?.label || '相关图形元素'}</button>
                }</For>
              </div>}</For>
              <Show when={quality().issues_truncated}><p>这里只显示部分问题，请结合完整检查结果复核。</p></Show>
              <Show when={quality().unchecked?.length}><p>尚未检查：{quality().unchecked!.join('；')}</p></Show>
              <p>{quality().note}</p>
            </>}</Show>
          </details>
          <details class="intelligent-canvas-elements intelligent-canvas-aux">
            <summary>精确选择元素</summary>
            <label>选择部件或元素<select value={selected()} onChange={e => choose(e.currentTarget.value)}>
              <option value="" selected={!selected()}>请选择</option>
              <For each={current().elements}>{item => <option value={item.element_id} selected={selected() === item.element_id}>{current().parts.find(p => p.element_id === item.element_id)?.label || `图形元素 · ${item.text||current().elements.indexOf(item)+1}`}</option>}</For>
            </select></label>
          </details>
          <Show when={research.latest?.enabled}>
            <details class="intelligent-canvas-aux" onToggle={e => { if (e.currentTarget.open) void refetchResearch(); }}><summary>关联研究课题</summary>
              <label>研究课题<select value={researchId()} onChange={e => setResearchId(e.currentTarget.value)}>
                <option value="" selected={!researchId()}>请选择课题</option>
                <For each={research.latest?.projects}>{p => <option value={p.research_id} selected={p.research_id === researchId()}>{p.title}</option>}</For>
              </select></label>
              <p class="intelligent-canvas-hint">保存正在查看的版本到课题。后续编辑不改变已保存的快照；标准研究包可以携带此图。</p>
              <Show when={research.latest?.record}><p role="status">{research.latest?.record?.artifact_refs[0]?.revision_id === current().revision_id ? '此版本已保存到课题' : '课题保存的是其他版本，可保存当前版本更新'}</p></Show>
              <button type="button" disabled={busy() || research.loading || !researchId() || props.allowSessionActions === false || sessionStore.currentSessionId() !== props.block.session_id}
                onClick={async () => { setBusy(true); setError(''); try {
                  await canvasRequest(props.block.canvas_id, props.block.session_id, '/research', {
                    research_id: researchId(), revision_id: current().revision_id, expected_revision: research.latest?.record?.revision || 0,
                  }); await refetchResearch(); setNotice('画布版本已保存到研究课题。');
                } catch (e) { setError(e instanceof Error ? e.message : '关联失败'); await refetchResearch(); } finally { setBusy(false); } }}>保存此版本到课题</button>
            </details>
          </Show>
          <details class="intelligent-canvas-aux"><summary>版本与恢复</summary><RevisionDeletion current={current().current_revision_id} viewed={current().revision_id}
            canWrite={props.allowSessionActions!==false&&!busy()&&sessionStore.currentSessionId()===props.block.session_id}
            request={(suffix,body)=>canvasRequest(props.block.canvas_id,props.block.session_id,suffix,body)}
            onChange={async(deleted,adjacent)=>{if(deleted)setRevision(adjacent||'latest');setHistoryBefore('');await refetch();await refetchHistory();}} />
          <button type="button" disabled={!canEdit() || !current().parent_revision_id} onClick={() => void commit('/restore', { base_revision_id: current().revision_id, revision_id: current().parent_revision_id, request_id: crypto.randomUUID() })}>恢复上一个版本</button>
          <Show when={props.onSaveHandoff}><button type="button" disabled={!canEdit()} onClick={async () => {
            setBusy(true); setError(''); try { await props.onSaveHandoff?.(current()); setNotice('此版本已保存到接力记录，可再次导出研究包。'); }
            catch (e) { setError(e instanceof Error ? e.message : '接力记录保存失败'); } finally { setBusy(false); }
          }}>保存此版本到接力记录</button></Show>
          <div class="intelligent-canvas-history-controls">
            <Show when={history.loading}><p role="status">正在加载版本…</p></Show>
            <Show when={history.error}><p role="alert">版本记录加载失败。</p><button type="button" onClick={() => void refetchHistory()}>重试加载版本</button></Show>
            <Show when={!history.error && history.latest}>{list => <>
              <p class="intelligent-canvas-hint">查看历史不会修改当前图。恢复会保存一个新版本，之后仍可回到其他版本。</p>
              <p>使用标题旁的版本下拉切换；这里可恢复版本或加载更早记录。</p>
              <Show when={historyBefore()}><button type="button" onClick={() => setHistoryBefore('')}>最近版本</button></Show>
              <Show when={list().next_before}><button type="button" onClick={() => setHistoryBefore(list().next_before)}>更早版本</button></Show>
            </>}</Show>
            <Show when={current().revision_id !== current().current_revision_id}>
              <button type="button" disabled={busy() || props.allowSessionActions === false || sessionStore.currentSessionId() !== props.block.session_id}
                onClick={() => void commit('/restore', { base_revision_id: current().current_revision_id, revision_id: current().revision_id, request_id: crypto.randomUUID() })}>恢复正在查看的版本</button>
            </Show>
          </div>
          </details>
      </div>
      </div>
</div>
      <Show when={current().revision_id !== current().current_revision_id}>
        <p class="intelligent-canvas-history">历史版本 · 选择最新版本后可继续修改</p>
      </Show>
      <Show when={mode()==='explore'&&current().parts.length}>
        <nav class="intelligent-canvas-breadcrumb" aria-label="整体、组件和零件层级">
          <button type="button" aria-current={!focus()?'location':undefined} onClick={whole}>返回整体</button>
          <For each={hierarchy().trail(focus())}>{(item,index)=><><span aria-hidden="true">›</span><button type="button" aria-current={focus()===item.element_id?'location':undefined} onClick={()=>choose(item.element_id,true)}>{index()===0?'组件':'零件'} · {item.label}</button></>}</For>
          <span class="intelligent-canvas-explore-state" role="status">{focus()?'局部聚焦 · 其他部件已淡化':'整体视图'}</span>
        </nav>
        <div class="intelligent-canvas-disassembly" role="group" aria-label="组件示意展开">
          <button type="button" aria-pressed={!!expandedComponent()} disabled={!expandedComponent()&&(!assembly()||expansionCount()<2||expansionCount()>12||!solidExpansionReady())}
            onClick={()=>{stopTimeline();setNotice('');setAppliedComponent('');if(expandedComponent())setExpandedComponent('');else {const id=assembly();setFocus(id);setSelected(id);setExpandedComponent(id);}}}>{expandedComponent()?'合拢组件':'展开组件'}</button>
          <span role="status">{expandedComponent()?`${appliedComponent()===expandedComponent()?'已展开':'正在展开'}：${current().parts.find(p=>p.element_id===expandedComponent())?.label||'当前组件'}`:transitionPhase()==='closing'?'正在合拢组件…':solidScene()&&!solidExpansionReady()?'此组件缺少空间拆解方向；可返回整体使用时间轴':'选择包含 2–12 个直属零件的组件后可展开'}</span>
          <small>仅分离显示已登记零件，不代表真实拆装顺序；连接线不会重新计算。查看操作不生成内容版本。</small>
        </div>
      </Show>
      <Show when={timelinePart()} keyed>{target=><CanvasTimeline part={target} style={style()} revision={current().revision_id}
        active={!collapsed()&&mode()==='explore'} resetToken={timelineReset()} canEdit={canEdit()}
        frame={()=>frame} nonce={()=>drawing()?.nonce||''}
        onFrame={state=>{setTimeline(state);if(state){setExpandedComponent('');const focus=state.focus?.[0]||state.id;setFocus(focus);setSelected(focus);}}}
        capture={solidScene()?async(animation)=>{const view=solidView();if(!view)throw Error('空间渲染尚未就绪，无法导出带遮挡的动画，请等待或重试。');return view.capture(animation);}:undefined}
        onCancelled={()=>setNotice('切换或关闭视图，已取消原来的GIF导出。')}
        onSave={animation=>void commit('/edits',{base_revision_id:current().revision_id,request_id:crypto.randomUUID(),element_ids:[target.element_id],operations:[{op:'set_animation',element_id:target.element_id,animation}]})} />}</Show>
      <Show when={!solidScene()&&current().parts.some(p=>p.illustration==='isometric')}>
        <p class="intelligent-canvas-history">旧版平面图未保存空间模型，前后遮挡可能不准确。请重新生成空间装配图；旧版本继续保留。</p>
      </Show>
      <div class="intelligent-canvas-body" data-editing={mode()==='edit'} data-selected={!!selected()}>
        <div class="intelligent-canvas-stage">
          <div hidden={!!solidScene()} class="intelligent-canvas-zoom" role="group" aria-label="图解缩放">
            <button type="button" aria-label="缩小图解" disabled={zoom() <= .25} onClick={() => setZoom(v => Math.max(.25, v - .25))}>−</button>
            <span aria-live="polite">{Math.round(zoom() * 100)}%</span>
            <button type="button" aria-label="放大图解" disabled={zoom() >= 4} onClick={() => setZoom(v => Math.min(4, v + .25))}>＋</button>
            <button type="button" onClick={() => setZoom(1)}>适应窗口</button>
          </div>
          <Show when={solidScene()} keyed>{scene=><SolidSceneViewer fixed={!solidMode()} scene={scene} share={solidShare()} target={solidTarget()||scene.id} selected={timeline()?.focus||selected()} onSelect={choose} ready={setSolidView}/>}</Show>
          <Show when={!solidScene()}><iframe ref={frame} title={props.block.title} sandbox="allow-scripts" srcdoc={drawing()?.html} onLoad={() => { setAppliedComponent('');updateViewport(); highlight(); }} /></Show>
          <Show when={current().parts.length}>
            <nav class="intelligent-canvas-parts" aria-label="部件导航">
              <For each={visibleParts()}>{item=><button type="button" aria-pressed={selected()===item.element_id} onClick={()=>choose(item.element_id,true)}>{item.label}</button>}</For>
            </nav>
          </Show>
        </div>
        <aside class="intelligent-canvas-inspector" aria-label="选区说明与编辑" on:wheel={passVerticalScrollToParent}>
          <Show when={selected()}>
            <Show when={mode()==='explore'}><div class="intelligent-canvas-detail" aria-live="polite">
              <span class="intelligent-canvas-eyebrow">{partIndex() >= 0 ? `${String(partIndex() + 1).padStart(2, '0')} / ${current().parts.length}` : '选中元素'}</span>
              <h4>{part()?.label || '所选图形元素'}</h4>
              <p>{part()?.description || '该元素没有说明，可选中后继续提问。'}</p>
            </div>
            </Show><Show when={mode()==='explore' && partIndex() >= 0}>
              <div class="intelligent-canvas-stepper">
                <button type="button" disabled={visibleParts().findIndex(p=>p.element_id===selected()) <= 0} onClick={() => step(-1)}>上一个</button>
                <button type="button" disabled={visibleParts().findIndex(p=>p.element_id===selected()) >= visibleParts().length - 1} onClick={() => step(1)}>下一个</button>
              </div>
            </Show>
            <Show when={mode() === 'edit' && (!solidScene() || part())}>
              <div class="intelligent-canvas-edit">
                <h5>局部编辑</h5>
                <Show when={solidScene()}><p class="intelligent-canvas-hint">修改部件后，图解与三维同步更新。</p></Show>
                <Show when={part()}>
                  <label>部件名称<input value={label()} maxLength={256} onInput={e => setLabel(e.currentTarget.value)} disabled={!canEdit()} /></label>
                  <label>部件说明<textarea value={description()} maxLength={4000} onInput={e => setDescription(e.currentTarget.value)} disabled={!canEdit()} /></label>
                  <button type="button" disabled={!canEdit() || !label().trim() || (label() === part()?.label && description() === part()?.description)} onClick={() => {
                    const operations: CanvasOperation[] = [];
                    if(label() !== part()?.label) operations.push({op:'set_label',element_id:selected(),value:label()});
                    if(description() !== part()?.description) operations.push({op:'set_description',element_id:selected(),value:description()});
                    void commit('/edits',{base_revision_id:current().revision_id,request_id:crypto.randomUUID(),element_ids:[selected()],operations});
                  }}>保存名称与说明</button>
                </Show>
                <div class="intelligent-canvas-color-row">
                  <label>颜色<input type="color" value={color()} onInput={e => setColor(e.currentTarget.value)} disabled={!canEdit()} /></label>
                  <Show when={!solidScene()} fallback={<span>部件颜色</span>}><label>应用于<select value={colorTarget()} onChange={e => setColorTarget(e.currentTarget.value as 'fill' | 'stroke')} disabled={!canEdit()}><option value="fill">填充</option><option value="stroke">线条</option></select></label></Show>
                  <button type="button" disabled={!canEdit()} onClick={() => edit({ op: 'set_attribute', element_id: selected(), name: solidScene()?'fill':colorTarget(), value: color() })}>应用颜色</button>
                </div>
                <p class="intelligent-canvas-hint">{solidScene()?'部件颜色同步到图解与三维，保留材质明暗。':'分组改色保留文字颜色；轮廓图请选择线条。'}</p>
                <Show when={element()?.tag === 'text'}>
                  <label>图中文字<input value={text()} maxLength={4000} onInput={e => setText(e.currentTarget.value)} disabled={!canEdit()} /></label>
                  <button type="button" disabled={!canEdit()} onClick={() => edit({ op: 'set_text', element_id: selected(), value: text() })}>保存文字</button>
                </Show>
                <Show when={solidScene()}>
                  <details><summary>移动三维部件</summary>
                    <div class="intelligent-canvas-position intelligent-canvas-position-solid"><For each={['左右 X','前后 Y','上下 Z']}>{(axis,index)=><label>{axis}<input type="number" min="-200" max="200" step="any" value={solidOffset()[index()]} disabled={!canEdit()} onInput={e=>setSolidOffset(values=>values.map((v,i)=>i===index()?e.currentTarget.value:v))} /></label>}</For></div>
                    <p class="intelligent-canvas-hint">相对当前装配位置移动，图解与三维同步；保存后可检查拆解路径。</p>
                    <button type="button" disabled={!canEdit() || !validSolidTranslation()} onClick={()=>edit({op:'translate_solid',element_id:selected(),vector:solidTranslation()})}>保存三维位置</button>
                  </details>
                </Show>
                <Show when={position() && !solidScene()}>
                  <details>
                    <summary>调整位置</summary>
                    <div class="intelligent-canvas-position"><label>水平偏移<input type="number" min={-100000} max={100000} step="any" value={offsetX()} onInput={e => setOffsetX(e.currentTarget.value)} disabled={!canEdit()} /></label>
                    <label>垂直偏移<input type="number" min={-100000} max={100000} step="any" value={offsetY()} onInput={e => setOffsetY(e.currentTarget.value)} disabled={!canEdit()} /></label></div>
                    <p class="intelligent-canvas-hint">单位为图内坐标，正数向右或向下；分组整体移动，连线不会自动重排。</p>
                    <button type="button" disabled={!canEdit() || !translation() || translation() === `translate(${position()?.[0]} ${position()?.[1]})`} onClick={() => edit({ op: 'set_attribute', element_id: selected(), name: 'transform', value: translation()! })}>保存位置</button>
                  </details>
                </Show>
              </div>
            </Show>
            <Show when={mode()==='edit' && solidScene() && !part()}><p>请从部件导航选择组件或零件，再同步编辑图解与三维。</p></Show>
            <details class="intelligent-canvas-ask"><summary>提问或让 Agent 修改</summary><div class="intelligent-canvas-ask-content">
              <label>对选区提问或要求修改<textarea value={instruction()} maxLength={4000} onInput={e => setInstruction(e.currentTarget.value)} placeholder="例如：改成蓝色，并同步这段部件说明" /></label>
              <button class="intelligent-canvas-primary" type="button" disabled={!canEdit() || !instruction().trim()} onClick={askAgent}>带选区继续对话</button>
            </div></details>
          </Show>
        </aside>
      </div>
      <div class="intelligent-canvas-feedback">
          <Show when={notice()}><p class="intelligent-canvas-notice" role="status">{notice()}</p></Show>
          <Show when={error()}><p class="intelligent-canvas-error" role="alert">{error()}</p></Show>
      </div>
      <footer class="intelligent-canvas-footer"><span>Agent 生成 · 内容待核对</span><details><summary>编辑须知</summary><p>视觉修改不会自动更新说明；可在编辑中修正所选部件说明。</p></details></footer>
    </>}</Show>
    </div>
  </section>;
}
