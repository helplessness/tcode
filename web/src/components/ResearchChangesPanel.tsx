import { For, Show, createEffect, createMemo, createResource, createSignal, on, onCleanup } from 'solid-js';
import type { ResearchProject } from '../api/research';
import { compareSnapshots, listSnapshots, openResearchChanges, snapshotRequest, type ResearchSnapshot, type SnapshotReceipt, type ResearchChangeFocus, type ResearchChangesLink } from '../api/researchChanges';
import { researchFieldLabels, researchFieldText } from '../utils/researchSnapshots';
import { comparisonChangeLabel, snapshotComparison } from '../utils/researchComparison';
import { confirmAction } from './FeedbackDialog';
import ResearchMap from './ResearchMap';
import './ResearchChangesPanel.css';
import './ResearchSummaryEntry.css';

const kindLabels={project:'课题',hypothesis:'假说',experiment:'实验',finding:'结论',conflict:'冲突'};
export default function ResearchChangesPanel(props:{project:ResearchProject;workspaceId?:string;focus?:ResearchChangeFocus}) {
  const [offset,setOffset]=createSignal(0), [hidden,setHidden]=createSignal(false);
  const [base,setBase]=createSignal(''), [head,setHead]=createSignal(''), [changeOffset,setChangeOffset]=createSignal(0);
  const [title,setTitle]=createSignal(''), [busy,setBusy]=createSignal(false), [error,setError]=createSignal('');
  const [mapTarget,setMapTarget]=createSignal<{id:string;side:'before'|'after';key:number}>();
  const mapOpen=createMemo(()=>!!mapTarget()),mapKey=createMemo(()=>mapTarget()?.key);
  const [versions,{refetch}]=createResource(()=>[props.project.research_id,props.workspaceId,offset(),hidden()] as const, ([id,ws,offset,hidden])=>listSnapshots(id,ws,offset,hidden));
  const reversed=()=>{const rows=versions.error?[]:versions()?.items||[];const b=rows.find(s=>s.snapshot_id===base()),h=rows.find(s=>s.snapshot_id===head());return !!b&&!!h&&b.captured_at>h.captured_at;};
  const [comparison]=createResource(()=>base()&&head()&&base()!==head()&&!reversed()?[base(),head(),props.workspaceId,changeOffset()] as const:false, async ([b,h,ws,o])=>{const value=await compareSnapshots(b,h,ws,o);if(value.research_id!==props.project.research_id)throw new Error('版本不属于当前课题');return value;});
  const [mapSnapshots]=createResource(()=>mapOpen()?[base(),head(),props.workspaceId] as const:false,async ([beforeId,afterId,ws])=>{
    const [before,after]=await Promise.all([beforeId,afterId].map(id=>snapshotRequest<ResearchSnapshot>(`snapshots/${encodeURIComponent(id)}`,ws)));
    if([before,after].some(value=>value.research_id!==props.project.research_id))throw new Error('版本不属于当前课题');
    return {before,after,comparison:snapshotComparison(before.document.objects,after.document.objects)};
  });
  const mapSnapshot=()=>mapSnapshots.error?undefined:mapTarget()?.side==='before'?mapSnapshots()?.before:mapSnapshots()?.after;
  createEffect(on(()=>props.project.research_id,()=>{setBase('');setHead('');setOffset(0);setChangeOffset(0);setMapTarget(undefined);setError('');}, {defer:true}));
  createEffect(()=>{const f=props.focus;if(f){setBase(f.baseId);setHead(f.headId);setChangeOffset(0);setMapTarget({id:f.nodeId||'project',side:f.side||'after',key:f.key});}});
  let comparisonBody:HTMLDivElement|undefined;
  createEffect(()=>{
    const key=mapKey(), snapshot=mapSnapshots.error?undefined:mapSnapshots();
    if(!key||!snapshot||mapSnapshots.loading)return;
    const frame=requestAnimationFrame(()=>comparisonBody?.scrollIntoView({block:'start',behavior:'instant'}));
    onCleanup(()=>cancelAnimationFrame(frame));
  });
  const list=()=>versions.error?undefined:versions();
  const delta=()=>comparison.error?undefined:comparison();
  const canCompare=()=>!!base()&&!!head()&&base()!==head()&&!reversed();
  const choose=(side:'base'|'head',value:string)=>{side==='base'?setBase(value):setHead(value);setChangeOffset(0);setMapTarget(undefined);};
  const capture=async()=>{
    setBusy(true);setError('');
    try {const saved=await snapshotRequest<SnapshotReceipt>(`projects/${encodeURIComponent(props.project.research_id)}/snapshots`,props.workspaceId,{title:title().trim(),request_id:crypto.randomUUID()},'POST');setHead(saved.snapshot_id);setMapTarget(undefined);setTitle('');setOffset(0);setHidden(false);await refetch();}
    catch(e){setError(e instanceof Error?e.message:'保存失败');}finally{setBusy(false);}
  };
  const listingAction=async()=>{
    if(!head())return;
    if(!hidden()&&!await confirmAction('将此版本移出列表？保存内容不会删除，之后可以从“已移出版本”恢复。'))return;
    setBusy(true);setError('');
    try {await snapshotRequest(`snapshots/${encodeURIComponent(head())}${hidden()?'/restore':''}`,props.workspaceId,undefined,hidden()?'POST':'DELETE');setHead('');setMapTarget(undefined);await refetch();}
    catch(e){setError(e instanceof Error?e.message:'操作失败');}finally{setBusy(false);}
  };
  const mapProject=createMemo(()=>{const root=mapSnapshot()?.document.objects.find(o=>o.kind==='project');return {...props.project,title:root?.title||props.project.title,description:String(root?.data.description||'')};});
  const missingTarget=()=>!!mapSnapshot()&&!mapSnapshots()!.comparison.nodeIds[mapTarget()!.side].has(mapTarget()!.id);
  return <section class="research-changes" aria-label="研究变化比较">
    <header><div><h3>研究变化比较</h3><p>选择两个已保存版本，查看新增、删除、内容修改和关系修改。</p></div></header>
    <details class="research-changes-management"><summary>保存与管理版本</summary>
    <form class="research-changes-capture" onSubmit={e=>{e.preventDefault();void capture();}}><label>版本名称<input value={title()} maxLength={160} placeholder="例如：完成第一轮核对" onInput={e=>setTitle(e.currentTarget.value)} /></label><button disabled={busy()||!title().trim()}>保存当前研究版本</button></form>
    <label class="research-changes-hidden"><input type="checkbox" checked={hidden()} onChange={e=>{setHidden(e.currentTarget.checked);setOffset(0);}} />显示已移出版本</label>
    <div class="research-changes-actions"><button disabled={!offset()||versions.loading} onClick={()=>setOffset(Math.max(0,offset()-50))}>上一页版本</button><button disabled={list()?.next_offset==null||versions.loading} onClick={()=>setOffset(list()!.next_offset!)}>下一页版本</button><button disabled={!head()||busy()||!list()?.items.some(s=>s.snapshot_id===head())} onClick={()=>void listingAction()}>{hidden()?'恢复所选版本':'移出比较后版本'}</button></div>
    </details>
    <Show when={versions.loading}><p role="status">正在加载版本…</p></Show><Show when={versions.error}><p role="alert">{String(versions.error.message)}</p></Show>
    <div class="research-changes-selectors"><For each={['base','head'] as const}>{side=><label>{side==='base'?'基线版本 · 比较前':'对照版本 · 比较后'}<select aria-label={side==='base'?'比较前研究版本':'比较后研究版本'} value={side==='base'?base():head()} disabled={busy()||versions.loading} onChange={e=>choose(side,e.currentTarget.value)}><option value="" selected={!(side==='base'?base():head())}>选择已保存版本</option><Show when={(side==='base'?base():head())&&!list()?.items.some(s=>s.snapshot_id===(side==='base'?base():head()))}><option value={side==='base'?base():head()} selected>{(side==='base'?delta()?.base.title:delta()?.head.title)||'已选版本'}</option></Show><For each={list()?.items||[]}>{s=><option value={s.snapshot_id} selected={s.snapshot_id===(side==='base'?base():head())}>{s.title} · {new Date(s.captured_at).toLocaleString()}</option>}</For></select></label>}</For><Show when={reversed()}><button type="button" disabled={busy()} onClick={()=>{const old=base();choose('base',head());choose('head',old);}}>按时间排序</button></Show></div>
    <Show when={!canCompare()&&!versions.loading}><div class="research-changes-start"><h4>{reversed()?'基线版本需要早于对照版本':base()&&base()===head()?'请选择两个不同版本':'从一次版本比较开始'}</h4><p>先选基线，再选对照版本。保存与管理操作收在上方，不会修改原研究。</p><Show when={(list()?.items.length||0)>=2}><button type="button" onClick={()=>{choose('base',list()!.items[1].snapshot_id);choose('head',list()!.items[0].snapshot_id);}}>比较列表中最近两个版本</button></Show></div></Show>

    <Show when={!versions.loading&&!list()?.items.length}><p>还没有{hidden()?'已移出':'已保存'}的研究版本。</p></Show>
    <Show when={error()}><p role="alert">{error()}</p></Show><Show when={comparison.error}><p role="alert">{String(comparison.error.message)}</p></Show><Show when={comparison.loading}><p role="status">正在计算差异…</p></Show>
    <div class="research-changes-body"><div class="research-changes-diff"><Show when={canCompare()&&!comparison.loading&&delta()}>{data=><><div class="research-changes-counts" role="status"><strong>{data().total} 项变化</strong><span>新增 {data().counts.added}</span><span>修改 {data().counts.changed}</span><span>删除 {data().counts.removed}</span><span>来源变化 {data().source_changed_count}</span></div><p class="research-changes-note">仅比较已保存内容，不代表计算已运行或研究结论已验证。</p>
      <Show when={!data().total}><p>两个版本的已保存研究字段没有变化。</p></Show>
      <div class="research-changes-list"><For each={data().changes}>{change=><article data-status={change.status} data-selected={mapTarget()?.id===change.id}><header><div><small>{comparisonChangeLabel(change)} · {kindLabels[change.kind]}{change.source_changed?' · 来源引用有变化':''}</small><h4>{change.title}</h4><p class="research-changes-field-summary">{change.fields.map(field=>researchFieldLabels[field]||'补充信息').join('、')}</p></div><button type="button" onClick={()=>setMapTarget({id:change.id,side:change.after?'after':'before',key:Date.now()})}>在地图中查看</button></header><details><summary>展开前后内容 · {change.fields.length} 个字段</summary><For each={change.fields}>{field=><div class="research-changes-field"><strong>{researchFieldLabels[field]||'补充信息'}</strong><div><pre aria-label="比较前内容">{change.before?researchFieldText(field,change.before[field],change.before_names):'此版本不存在'}</pre><pre aria-label="比较后内容">{change.after?researchFieldText(field,change.after[field],change.after_names):'此版本不存在'}</pre></div></div>}</For></details></article>}</For></div>
      <div class="research-changes-actions"><button disabled={!changeOffset()||comparison.loading} onClick={()=>setChangeOffset(Math.max(0,changeOffset()-50))}>上一页变化</button><button disabled={data().next_offset==null||comparison.loading} onClick={()=>setChangeOffset(data().next_offset!)}>下一页变化</button></div>
    </>}</Show>
    </div><Show when={mapTarget()}><div ref={comparisonBody} class="research-changes-map-pane"><header class="research-changes-map-toolbar"><div><h4>变化所在位置</h4><small>只读历史地图。结构分析共用位置基准，空位表示该版本没有此对象；关系修改不会挪动原对象，归属以连线和记录为准。</small></div><div class="research-changes-actions" role="group" aria-label="地图版本"><button type="button" aria-pressed={mapTarget()?.side==='before'} onClick={()=>setMapTarget({...mapTarget()!,side:'before',key:Date.now()})}>比较前地图</button><button type="button" aria-pressed={mapTarget()?.side==='after'} onClick={()=>setMapTarget({...mapTarget()!,side:'after',key:Date.now()})}>比较后地图</button><button type="button" onClick={()=>setMapTarget(undefined)}>收起地图</button></div></header><Show when={mapSnapshots.loading}><p role="status">正在读取历史地图…</p></Show><Show when={mapSnapshots.error}><p role="alert">{String(mapSnapshots.error.message)}</p></Show>
    <Show when={!mapSnapshots.loading&&!mapSnapshots.error&&mapSnapshots()}>{pair=><div class="research-changes-map"><p>{mapSnapshot()!.title} · {new Date(mapSnapshot()!.captured_at).toLocaleString()}</p><div class="research-changes-map-legend" aria-label="变化标记"><span>＋ 新增</span><span>− 删除</span><span>内容修改</span><span>↔ 关系修改</span></div><Show when={missingTarget()}><p role="status" class="research-changes-note">{mapTarget()?.side==='before'?'此对象在比较前尚未新增，可切换比较后地图查看。':'此对象已从比较后版本删除，可切换比较前地图查看。'}</p></Show><ResearchMap project={mapProject()} workspaceId={props.workspaceId} initialView="analysis" initialSelected={mapTarget()?.id} snapshotObjects={mapSnapshot()!.document.objects} comparison={pair().comparison} onSelected={id=>setMapTarget(target=>target&&target.id!==id?{...target,id}:target)} /></div>}</Show>
    </div></Show></div>
  </section>;
}

export function ResearchChangesSummary(props:{link:ResearchChangesLink}) {
  const [data]=createResource(()=>props.link,async link=>{const value=await compareSnapshots(link.base_snapshot_id,link.head_snapshot_id,link.workspace_id);if(value.research_id!==link.research_id)throw new Error('比较与课题引用不一致');return value;});
  const [opening,setOpening]=createSignal(false);
  const open=async(id='project',side:'before'|'after'='after')=>{if(opening())return;setOpening(true);try{await openResearchChanges(props.link,id,side);}finally{setOpening(false);}};
  return <details class="research-changes research-changes-summary research-summary-entry" aria-busy={opening()}><summary><span class="research-summary-title">研究变化摘要</span><small><span class="research-summary-expand">展开</span><span class="research-summary-collapse">收起</span></small></summary><Show when={data.loading}><p role="status">正在读取保存的比较…</p></Show><Show when={data.error}><p role="alert">比较不可用：{String(data.error.message)}</p></Show><Show when={!data.error&&data()}>{d=><><p>{d().base.title} → {d().head.title}</p><p>新增 {d().counts.added} · 修改 {d().counts.changed} · 删除 {d().counts.removed}</p><p>{d().history_notice}</p><div class="research-changes-list"><For each={d().changes.slice(0,5)}>{c=><button disabled={opening()} class="research-changes-summary-item" onClick={()=>open(c.id,c.status==='removed'?'before':'after')}><span><small>{comparisonChangeLabel(c)}</small><strong>{c.title}</strong></span><span aria-hidden="true">查看地图 →</span></button>}</For></div><button disabled={opening()} class="research-changes-open-all" onClick={()=>void open()}>{opening()?'正在打开研究中心…':'查看完整比较与地图 →'}</button><Show when={opening()}><p role="status">正在读取课题并打开历史地图…</p></Show></>}</Show></details>;
}
