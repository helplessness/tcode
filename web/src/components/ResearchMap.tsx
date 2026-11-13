import { For, Show, createEffect, createMemo, createSignal, createUniqueId, onCleanup, untrack } from 'solid-js';
import { getSessions, type SessionSummary } from '../api/sessions';
import { listResearchActivities, listResearchExperiments, type ResearchExperiment, type ResearchProject } from '../api/research';
import { experimentParents, researchMapAncestors, researchMapDescendantCounts, researchMapLayout, type MapPoint } from '../utils/researchMap';
import './ResearchMap.css';
import { snapshotMapNodes, researchFieldText } from '../utils/researchSnapshots';
import { comparisonLayout, comparisonLabels, type SnapshotComparison } from '../utils/researchComparison';
import type { ResearchSnapshotObject } from '../api/researchChanges';
import { render, delegateEvents, clearDelegatedEvents } from 'solid-js/web';
type Detail = MapPoint & { title: string; text: string; status?: string; fields?: Record<string, unknown> };
const labels: Record<string, string> = { active: '进行中', archived: '已归档', planned: '待执行', running: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消', paused: '已暂停' };
const kinds = { project: '课题', session: '研究会话', experiment: '实验', group: '未关联会话', hypothesis:'假说记录', finding:'研究结论', conflict:'结论冲突' };
// Decorative, deterministic sphere geometry; it does not represent research relationships.
const spherePoints = Array.from({ length: 42 }, (_, i) => {
  const y = 1 - 2 * (i + .5) / 42, radius = Math.sqrt(1 - y * y), angle = i * 2.399963;
  return { x: 100 + Math.cos(angle) * radius * 72, y: 100 + y * 72, z: Math.sin(angle) * radius };
});
function EnergySphere(props: { selected: boolean }) {
  const id = createUniqueId();
  return <svg viewBox="0 0 200 200" class="research-map-glass" aria-hidden="true">
    <defs>
      <radialGradient id={`${id}-shell`} cx="30%" cy="24%" r="78%"><stop stop-color="#ffffff" stop-opacity=".95" /><stop offset=".3" stop-color="#dfe2e8" stop-opacity=".3" /><stop offset=".75" stop-color="#92949f" stop-opacity=".45" /><stop offset="1" stop-color="#5a606d" stop-opacity=".7" /></radialGradient>
      <radialGradient id={`${id}-core`}><stop stop-color="#fffef1" /><stop offset=".18" stop-color="#fff1c6" /><stop offset=".4" stop-color="#ff8150" /><stop offset=".7" stop-color="#e93728" stop-opacity=".65" /><stop offset="1" stop-color="#c8232a" stop-opacity="0" /></radialGradient>
      <clipPath id={`${id}-clip`}><circle cx="100" cy="100" r="76" /></clipPath>
    </defs>
    <circle class="research-map-glass-shell" cx="100" cy="100" r="78" fill={`url(#${id}-shell)`} />
    <g clip-path={`url(#${id}-clip)`}>
      <g class="research-map-core"><circle cx="105" cy="112" r="64" fill={`url(#${id}-core)`} /><circle cx="105" cy="112" r="13" fill="#fff9dc" /></g>
      <g class="research-map-neural-mesh"><For each={spherePoints}>{(p, i) => <For each={spherePoints.slice(i() + 1).filter(q => Math.hypot(p.x - q.x, p.y - q.y) < 43)}>{q => <path d={`M${p.x},${p.y} L${q.x},${q.y}`} opacity={.25 + (p.z + 1) * .22} />}</For>}</For></g>
      <g class="research-map-particles"><For each={spherePoints}>{(p, i) => <circle cx={p.x} cy={p.y} r={i() % 7 === 0 ? 2.1 : 1} style={{ 'animation-delay': `${-i() * .37}s` }} />}</For></g>
      <path class="research-map-core-flare" d="M81 95 Q101 111 111 139 L124 144" />
    </g>
    <ellipse cx="76" cy="49" rx="29" ry="10" fill="#fff" opacity=".65" transform="rotate(-30 76 49)" />
    <circle cx="100" cy="100" r="79" fill="none" stroke="#fff" stroke-width="3" opacity=".85" />
    <circle cx="100" cy="100" r="83" fill="none" stroke="#aeb4bf" stroke-width="1" opacity=".55" />
    <Show when={props.selected}><g class="research-map-rotating-ring"><circle cx="100" cy="100" r="94" class="research-map-ring-track" /><circle cx="100" cy="100" r="94" class="research-map-ring-segments" /></g></Show>
  </svg>;
}
export default function ResearchMap(props: { project: ResearchProject; workspaceId?: string; standalone?: boolean; initialView?: 'overview' | 'analysis'; initialSelected?: string; snapshotObjects?:ResearchSnapshotObject[]; comparison?:SnapshotComparison; onSelected?:(id:string)=>void }) {
  const id = createUniqueId();
  const [sessions, setSessions] = createSignal<SessionSummary[]>([]), [experiments, setExperiments] = createSignal<ResearchExperiment[]>([]);
  const [selected, setSelected] = createSignal('project'), [detailOpen, setDetailOpen] = createSignal(false);
  const [hovered, setHovered] = createSignal('');
  const [expanded, setExpanded] = createSignal(new Set(['project']));
  const [loading, setLoading] = createSignal(true), [error, setError] = createSignal('');
  const [revision, setRevision] = createSignal(0), [floating, setFloating] = createSignal(true), [zoom, setZoom] = createSignal(.75);
  const [view, setView] = createSignal(props.initialView || 'overview');
  const [analysisExpanded, setAnalysisExpanded] = createSignal(new Set(['project']));
  const [analysisZoom, setAnalysisZoom] = createSignal(.85);
  const analysis = () => view() === 'analysis';
  const currentExpanded = () => analysis() ? analysisExpanded() : expanded();
  const updateExpanded = (value: Set<string>) => analysis() ? setAnalysisExpanded(value) : setExpanded(value);
  const currentZoom = () => analysis() ? analysisZoom() : zoom();
  const updateZoom = (value: number) => analysis() ? setAnalysisZoom(value) : setZoom(value);
  let canvas: HTMLDivElement | undefined;
  let mapElement: HTMLElement | undefined;
  const [enlarged, setEnlarged] = createSignal(false);
  let locateFrame = 0;
  const [windowNotice, setWindowNotice] = createSignal('');
  let generation = 0;
  let popup: Window | null = null;
  let disposePopup: (() => void) | undefined;
  const closePopup = () => { disposePopup?.(); disposePopup = undefined; if (popup && !popup.closed) { clearDelegatedEvents(popup.document); popup.close(); } popup = null; };
  onCleanup(() => { generation++; cancelAnimationFrame(locateFrame); closePopup(); });
  const openWindow = () => {
    setWindowNotice('');
    if (mapElement?.matches(':popover-open')) { mapElement.hidePopover(); return; }
    if (popup && !popup.closed) { popup.focus(); return; }
    disposePopup?.();
    popup = window.open('', 'tcode-research-map', 'popup,width=1280,height=900');
    if (!popup) {
      mapElement?.setAttribute('popover', 'auto'); mapElement?.showPopover();
      setWindowNotice('当前环境不支持独立窗口，已在应用内放大。'); locate(); return;
    }
    const target = popup;
    // Solid 默认在主 document 委托事件，跨窗口渲染必须为目标 document 注册。
    delegateEvents(['click', 'keydown', 'mouseover', 'mouseout'], target.document);
    target.opener = null;
    target.document.title = `${props.project.title} · 研究地图`;
    target.document.documentElement.lang = 'zh-CN';
    target.document.documentElement.className = document.documentElement.className;
    for(const name of ['data-desktop','data-reduce-motion']) { const value=document.documentElement.getAttribute(name);if(value!==null)target.document.documentElement.setAttribute(name,value); }
    for (const style of document.querySelectorAll('link[rel="stylesheet"], style')) target.document.head.appendChild(style.cloneNode(true));
    const mount = target.document.createElement('main');
    mount.className = 'research-map-window'; target.document.body.appendChild(mount);
    disposePopup = render(() => <><button type="button" onClick={() => target.close()}>关闭独立窗口</button><ResearchMap project={props.project} workspaceId={props.workspaceId} standalone initialView={view()} initialSelected={selected()} snapshotObjects={props.snapshotObjects} comparison={props.comparison} /></>, mount);
  };
  createEffect(() => {
    const project = props.project, workspace = props.workspaceId;
    revision(); const request = ++generation;
    if (popup && !popup.closed) closePopup();
    setLoading(true); setError(''); setSessions([]); setExperiments([]);
    setHovered(''); setWindowNotice('');
    if(!props.comparison) {setSelected('project');setDetailOpen(false);setExpanded(new Set(['project']));setAnalysisExpanded(new Set(['project']));}
    if (props.snapshotObjects) {
      setLoading(false);
      if (props.initialSelected && nodes().some(n=>n.id===props.initialSelected)) {setSelected(props.initialSelected);setDetailOpen(true);untrack(locate);}
      else if(props.initialSelected && props.initialSelected!=='project') {setSelected('project');setDetailOpen(false);setWindowNotice('所选对象不在此保存版本中。地图保留其位置空位，可切换另一版本查看。');}
      return;
    }
    void Promise.all([getSessions(), getSessions({ archived: true }), listResearchActivities(workspace, 'included'), listResearchExperiments(project.research_id, workspace)]).then(([current, archived, activities, result]) => {
      if (request !== generation) return;
      const linked = new Set(activities.activities.filter(a => a.research_id === project.research_id).flatMap(a => a.session_ids));
      result.experiments.forEach(e => { if (e.run_snapshot?.session_id) linked.add(e.run_snapshot.session_id); });
      const all = new Map([...current, ...archived].map(s => [s.session_id, s]));
      const visible = [...all.values()].filter(s => s.research_id === project.research_id || linked.has(s.session_id));
      for (const id of linked) if (!all.has(id)) visible.push({ session_id: id, title: '来源会话（不可用）' } as SessionSummary);
      setSessions(visible); setExperiments(result.experiments);
      if (props.initialSelected && nodes().some(n => n.id === props.initialSelected)) {
        setSelected(props.initialSelected); setDetailOpen(true); locate();
      }
    }).catch(e => { if (request === generation) setError(e instanceof Error ? e.message : '地图加载失败'); }).finally(() => { if (request === generation) setLoading(false); });
  });
  const nodes = createMemo<Detail[]>(() => {
    if (props.snapshotObjects) return snapshotMapNodes(props.snapshotObjects);
    const p = props.project;
    const rows: Detail[] = [{ id: 'project', parent: '', kind: 'project', title: p.title, text: p.description, status: p.status, fields: {} }];
    sessions().forEach(s => rows.push({ id: `session:${s.session_id}`, parent: 'project', kind: 'session', title: s.title || '未命名会话', text: s.message_preview || '通过课题归属、研究活动或实验执行记录关联。', status: s.is_archived ? 'archived' : s.status, fields: { '消息数': s.message_count, 'Agent': s.agent_name } }));
    const parents = experimentParents(experiments());
    if (experiments().some(e => !parents.get(e.experiment_id) && !e.run_snapshot?.session_id)) rows.push({ id: 'unlinked', parent: 'project', kind: 'group', title: '未关联会话的实验', text: '这些实验没有记录来源会话，保留在课题下，不推测会话归属。' });
    const append = (parent: string, children: ResearchExperiment[]) => children.forEach(e => {
      rows.push({ id: `experiment:${e.experiment_id}`, parent, kind: 'experiment', title: e.title || '未命名实验', text: e.result_summary || e.reason || '尚未记录结果', status: e.status, fields: { '实验目的': e.reason, '判断': researchFieldText('decision',e.decision), '指标': researchFieldText('metrics',e.metrics), '计算运行': e.workflow_run_id ? '已关联' : '未关联', '更新时间': e.updated_at } });
      append(`experiment:${e.experiment_id}`, experiments().filter(child => parents.get(child.experiment_id) === e.experiment_id));
    });
    experiments().filter(e => !parents.get(e.experiment_id)).forEach(e => append(e.run_snapshot?.session_id ? `session:${e.run_snapshot.session_id}` : 'unlinked', [e]));
    return rows;
  });
  const layout = createMemo(() => props.comparison&&analysis()?comparisonLayout(props.comparison,nodes(),currentExpanded()):researchMapLayout(nodes(), currentExpanded(), analysis()));
  const changeMark=(nodeId:string)=>props.comparison?.marks.get(nodeId);
  const changeLabel=(nodeId:string)=>{const mark=changeMark(nodeId);return mark?mark.relationChanges.length?comparisonLabels.relation:comparisonLabels[mark.status]:'';};
  const detail = () => nodes().find(n => n.id === selected());
  const point = (id: string) => layout().points.get(id)!;
  const childCount = (id: string) => nodes().filter(n => n.parent === id).length;
  const descendantCounts = createMemo(() => researchMapDescendantCounts(nodes()));
  const collapsedCount = (id: string) => currentExpanded().has(id) ? 0 : descendantCounts().get(id) || 0;
  const relation = (n: Detail) => n.kind === 'experiment' && n.parent.startsWith('experiment:') ? '延续自' : n.kind === 'experiment' && n.parent.startsWith('session:') ? '来源会话' : '归属';
  const locate = () => {
    updateExpanded(new Set([...currentExpanded(), ...researchMapAncestors(nodes(), selected())]));
    cancelAnimationFrame(locateFrame);
    locateFrame = requestAnimationFrame(() => {
      const p = layout().points.get(selected());
      if (canvas && p) canvas.scrollTo({ left: Math.max(0, p.x * currentZoom() + 20 - canvas.clientWidth / 2), top: Math.max(0, p.y * currentZoom() + 50 - canvas.clientHeight / 2), behavior: 'instant' });
    });
  };
  const switchView = (next: 'overview' | 'analysis') => { setHovered(''); setView(next); locate(); };
  const toggleNode = (n: Detail) => {
    setSelected(n.id); setDetailOpen(true);
    if (childCount(n.id)) { const next = new Set(currentExpanded()); if (next.has(n.id)) next.delete(n.id); else next.add(n.id); updateExpanded(next); }
    props.onSelected?.(n.id);
  };
  const overview = () => { updateExpanded(new Set(['project'])); setSelected('project'); setDetailOpen(false); locate(); };
  const linkPath = (n: Detail) => { const a = point(n.parent), b = point(n.id);
    if(analysis()&&a.x===b.x)return `M ${a.x+120} ${a.y} C ${a.x+200} ${a.y}, ${b.x+200} ${b.y}, ${b.x+120} ${b.y}`;
    const direction=a.x>b.x?-1:1,start=a.x+(analysis()?120*direction:0),end=b.x-(analysis()?128*direction:0);return `M ${start} ${a.y} C ${(start + end) / 2} ${a.y}, ${(start + end) / 2} ${b.y}, ${end} ${b.y}`; };
  const relationX=(n:Detail)=>point(n.parent).x===point(n.id).x?point(n.id).x+185:(point(n.parent).x+point(n.id).x)/2;
  return <section ref={mapElement} class="research-map" data-floating={floating()} data-view={view()} data-hovering={!!hovered()} aria-label="研究地图预览" onToggle={() => {
    const open = !!mapElement?.matches(':popover-open'); setEnlarged(open);
    if (!open) { mapElement?.removeAttribute('popover'); setWindowNotice(''); }
  }}>
    <Show when={windowNotice()}><p role="status">{windowNotice()}</p></Show>
    <header><Show when={!props.snapshotObjects||props.standalone}><div><h3>研究地图 <small>预览</small></h3><p>{analysis() ? '查看研究对象及已记录关系，点击节点核对详情。' : '点击光球或会话展开分支；点击实验查看详情。'}连线仅表示记录中的归属与延续。</p></div></Show><div class="research-map-actions"><button type="button" disabled={loading()} onClick={overview}>重置视图</button><Show when={!analysis()}><button type="button" aria-pressed={!floating()} title="暂停光球呼吸、节点游动与连线流光。系统开启减少动态效果时自动停止动画。" onClick={() => setFloating(v => !v)}>{floating() ? '暂停特效' : '继续特效'}</button></Show><button type="button" disabled={loading()} onClick={() => updateExpanded(new Set(nodes().map(n => n.id)))}>全部展开</button><button type="button" disabled={loading()} onClick={() => { updateExpanded(new Set<string>()); setDetailOpen(false); }}>全部折叠</button><button type="button" disabled={loading()} onClick={() => setRevision(v => v + 1)}>刷新地图</button><Show when={!props.standalone}><button type="button" class="research-map-popout" onClick={openWindow}>{enlarged() ? '退出放大' : '展开放大 · 独立窗口'}</button></Show></div></header>
    <div class="research-map-view-switch" role="group" aria-label="研究地图查看方式"><button type="button" aria-pressed={!analysis()} onClick={() => switchView('overview')}>研究全景</button><button type="button" aria-pressed={analysis()} onClick={() => switchView('analysis')}>结构分析</button><button type="button" disabled={loading() || !!error()} onClick={locate}>定位当前对象</button><span role="status">当前对象：{detail()?.title || props.project.title}</span></div>
    <Show when={analysis()}><div class="research-map-legend" aria-label="研究对象类型"><For each={[...new Set(nodes().map(n=>n.kind))]}>{kind=><span data-kind={kind}>{kinds[kind]}</span>}</For></div></Show>
    <Show when={loading()}><p role="status">正在读取会话与实验…</p></Show><Show when={error()}><p role="alert">{error()}，请刷新重试。</p></Show>
    <Show when={!loading() && !error()}><div class="research-map-stage" data-detail-open={detailOpen()} onKeyDown={e => { if (e.key === 'Escape' && !enlarged()) setDetailOpen(false); }}>
      <div class="research-map-stage-caption"><span>{props.snapshotObjects ? '已保存版本 · 归属与实验延续' : '课题 → 会话 → 实验'}</span><span>{props.snapshotObjects ? `${nodes().length} 个记录节点` : `${sessions().length} 个会话 / ${experiments().length} 个实验`}</span></div>
      <div ref={canvas} class="research-map-canvas" tabIndex={0} aria-label={analysis() ? '可滚动研究结构分析' : '可滚动动态研究地图'}><div style={{ width: `${layout().width * currentZoom()}px`, height: `${layout().height * currentZoom()}px` }}><div class="research-map-space" style={{ width: `${layout().width}px`, height: `${layout().height}px`, transform: `scale(${currentZoom()})` }}><div class="research-map-universe">
        <svg class="research-map-links" width={layout().width} height={layout().height} aria-hidden="true"><defs><marker id={`research-arrow-${id}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 1 1 L 9 5 L 1 9" fill="none" stroke="context-stroke" stroke-width="1.5" /></marker></defs><For each={nodes().filter(n => n.parent)}>{n => <g data-kind={n.kind} data-change={changeMark(n.id)?.parentChanged?'relation':changeMark(n.id)?.status==='changed'?undefined:changeMark(n.id)?.status} classList={{ visible: point(n.id).visible, highlighted: selected() === n.id || selected() === n.parent || hovered() === n.id || hovered() === n.parent }}><path class="research-map-link-base" d={linkPath(n)} marker-end={analysis()?`url(#research-arrow-${id})`:undefined} /><Show when={!analysis()}><path class="research-map-link-flow" d={linkPath(n)} pathLength="100" /></Show><Show when={analysis()}><text class="research-map-relation-label" x={relationX(n)} y={(point(n.parent).y + point(n.id).y) / 2 - 10} text-anchor="middle">{changeMark(n.id)?.parentChanged?'关系修改 · ':''}{relation(n)}</text></Show></g>}</For></svg>
        <For each={nodes()}>{n => <div class="research-map-position" data-change={changeMark(n.id)?.relationChanges.length?'relation':changeMark(n.id)?.status} data-kind={n.kind} data-visible={point(n.id).visible} data-related={!hovered()||n.id===hovered()||n.parent===hovered()||nodes().find(row=>row.id===hovered())?.parent===n.id} style={{ transform: `translate(${point(n.id).x}px, ${point(n.id).y}px)` }} inert={!point(n.id).visible} aria-hidden={!point(n.id).visible}>
          <button type="button" class="research-map-node" classList={{ selected: selected() === n.id }} aria-pressed={selected() === n.id} aria-label={`${kinds[n.kind]}：${n.title}${changeLabel(n.id)?`，${changeLabel(n.id)}`:''}${childCount(n.id) ? currentExpanded().has(n.id) ? '，点击折叠' : `，已折叠 ${collapsedCount(n.id)} 项，点击展开` : '，查看详情'}`} aria-expanded={childCount(n.id) ? currentExpanded().has(n.id) : undefined} tabIndex={point(n.id).visible ? 0 : -1} onMouseEnter={() => setHovered(n.id)} onMouseLeave={() => setHovered('')} onClick={() => toggleNode(n)}>
            <Show when={!analysis()}><span class="research-map-organism" aria-hidden="true"><EnergySphere selected={selected() === n.id} /></span></Show>
            <Show when={collapsedCount(n.id)}><span class="research-map-collapsed-count" title={`已折叠 ${collapsedCount(n.id)} 项子内容`} aria-hidden="true">{collapsedCount(n.id)}</span></Show>
            <span class="research-map-node-label"><small>{kinds[n.kind]}{n.status ? ` · ${labels[n.status] || researchFieldText('status',n.status)}` : ''}</small><strong>{n.title}</strong><Show when={analysis()&&changeLabel(n.id)}><span class="research-map-change-badge">{changeLabel(n.id)}</span></Show><Show when={analysis()}><span class="research-map-card-summary">{n.text || '暂无描述'}</span></Show></span>
          </button>
        </div>}</For>
      </div></div></div></div>
      <div class="research-map-zoom"><button type="button" aria-label="缩小地图" disabled={currentZoom() <= .4} onClick={() => updateZoom(Math.max(.4, currentZoom() - .1))}>−</button><span>{Math.round(currentZoom() * 100)}%</span><button type="button" aria-label="放大地图" disabled={currentZoom() >= 1.2} onClick={() => updateZoom(Math.min(1.2, currentZoom() + .1))}>＋</button></div>
      <aside class="research-map-detail" data-open={detailOpen()} inert={!detailOpen()} aria-hidden={!detailOpen()} aria-label="节点详情" aria-live="polite"><button type="button" class="research-map-detail-close" aria-label="关闭节点详情" onClick={() => setDetailOpen(false)}>×</button><Show when={detail()}>{d => <><small>{kinds[d().kind]}</small><h3>{d().title}</h3><Show when={changeLabel(d().id)}><p class="research-map-change-badge">{changeLabel(d().id)}</p><For each={changeMark(d().id)?.relationChanges}>{line=><p>{line}</p>}</For></Show><p>{d().text || '暂无描述'}</p><button type="button" class="research-map-detail-locate" onClick={() => switchView(analysis() ? 'overview' : 'analysis')}>{analysis() ? '在研究全景中定位' : '在结构分析中定位'}</button><Show when={analysis()}><h4>已记录关系</h4><Show when={d().parent}><p>{relation(d())}：<button type="button" class="research-map-related" onClick={() => { setSelected(d().parent); props.onSelected?.(d().parent); locate(); }}>{nodes().find(n => n.id === d().parent)?.title}</button></p></Show><For each={nodes().filter(n => n.parent === d().id)}>{n => <p>{relation(n)} ← <button type="button" class="research-map-related" onClick={() => { setSelected(n.id); props.onSelected?.(n.id); locate(); }}>{n.title}</button></p>}</For><Show when={!d().parent && !childCount(d().id)}><p>暂无已记录关系</p></Show></Show><Show when={d().status}><p>状态：{labels[d().status!] || researchFieldText('status',d().status)}</p></Show><Show when={Object.keys(d().fields||{}).length}><details class="research-map-metadata"><summary>更多记录字段</summary><For each={Object.entries(d().fields || {}).filter(([, v]) => v !== undefined && v !== null && v !== '' && (typeof v !== 'object' || Object.keys(v).length))}>{([key, value]) => <div><h4>{key}</h4><pre>{typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}</pre></div>}</For></details></Show></>}</Show></aside>
    </div><Show when={!props.snapshotObjects && !sessions().length && !experiments().length}><p>当前课题还没有关联会话或实验。纳入研究记录后，光球周围会出现对应节点。</p></Show></Show>
  </section>;
}
