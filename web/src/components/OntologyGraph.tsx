import { confirmAction } from './FeedbackDialog';
import {
  AutoAdaptLabel, Circle, ClickSelect, D3ForceLayout, Diamond, DragCanvas, DragElement, ExtensionCategory, Graph, Hexagon,
  HoverActivate, Label, Line, ProcessParallelEdges, Quadratic, Rect, ZoomCanvas, getExtension, register,
} from '@antv/g6/esm/exports';
import { UpdateRelatedEdge } from '@antv/g6/esm/transforms/update-related-edge';
import { CollapseExpandNode } from '@antv/g6/esm/transforms/collapse-expand-node';
import { CollapseExpandCombo } from '@antv/g6/esm/transforms/collapse-expand-combo';
import { GetEdgeActualEnds } from '@antv/g6/esm/transforms/get-edge-actual-ends';
import { ArrangeDrawOrder } from '@antv/g6/esm/transforms/arrange-draw-order';
import { light } from '@antv/g6/esm/themes';
import { Circle as CircleShape, Group, Line as LineShape, Path, Text } from '@antv/g';
import { For, Show, createEffect, createMemo, createSignal, onCleanup, onMount, on } from 'solid-js';
import OntologyConflicts from './OntologyConflicts';
import { conflictLabels, conflictText, relationshipColor } from '../utils/ontologyConflicts';
import { ontologyRequest } from '../api/ontology';
import './OntologyGraph.css';
import { openCitation } from '../utils/citations';

export type OntologyGraphNode = {
  id: string; label: string; type: string; cluster: string; scope: 'public' | 'private';
  importance: number; source_count?: number; neighbor_count?: number; distance: number | null; description?: string;
};
export type OntologyGraphEdge = {
  id: string; source: string; target: string; original_source?: string; original_target?: string; predicate: string; polarity: string;
  scope: 'public' | 'private'; conflict: boolean; source_id: string; source_revision?: number;
  conflict_categories?: string[]; comparison_truncated?: boolean; qualifiers?: Record<string, unknown>;
  counterparts?: { id: string; scope: string; polarity: string; qualifiers: Record<string, unknown>; condition_relation: string; source_id?: string; quote?: string; evidence_ids?: string[] }[];
  quote: string; locator: Record<string, unknown>; evidence_ids: string[];
};
export type OntologyGraphData = {
  nodes: OntologyGraphNode[]; edges: OntologyGraphEdge[]; center_id: string; has_private: boolean;
  truncated: boolean; status?: 'not_found' | 'truncated' | 'complete'; nodes_truncated?: boolean; edges_truncated?: boolean; public_release_id: string; private_revision: number;
};

export const graphCompletenessText = (data?: Pick<OntologyGraphData, 'status' | 'truncated' | 'edges_truncated'>) => {
  if (!data) return '';
  if (data.status === 'not_found') return '未找到匹配实体';
  if (data.edges_truncated) return '关系达到展示上限，当前图谱不完整，请缩小查询范围';
  if (data.truncated) return '已展示当前范围中排名靠前的实体，部分实体未显示';
  return '当前范围已完整展示';
};

export { conflictLabels, conflictText, relationshipColor } from '../utils/ontologyConflicts';

type DisplayNode = OntologyGraphNode & { aggregate?: boolean; count?: number };
type Selection = { kind: 'node'; value: DisplayNode } | { kind: 'edge'; value: OntologyGraphEdge };

const palette = ['#65784b', '#aa7045', '#527c87', '#816b9b', '#9a8750', '#5f728f', '#8b6267'];
const nodeShapes = ['circle', 'rect', 'diamond', 'hexagon'] as const;
type NodeShape = typeof nodeShapes[number];
const stableShape = (type: string): NodeShape => nodeShapes[Array.from(type || 'unknown').reduce((sum,char)=>((sum*31)+char.codePointAt(0)!)>>>0,0)%nodeShapes.length];
const shapeClip = (shape: NodeShape) => shape==='circle'?'circle(50%)':shape==='diamond'?'polygon(50% 0,100% 50%,50% 100%,0 50%)':shape==='hexagon'?'polygon(25% 4%,75% 4%,100% 50%,75% 96%,25% 96%,0 50%)':'inset(8% round 3px)';
const registerOnce = (category: ExtensionCategory, name: string, extension: any) => {
  if (!getExtension(category, name)) register(category, name, extension);
};
registerOnce(ExtensionCategory.NODE, 'circle', Circle);
registerOnce(ExtensionCategory.NODE, 'rect', Rect);
registerOnce(ExtensionCategory.NODE, 'diamond', Diamond);
registerOnce(ExtensionCategory.NODE, 'hexagon', Hexagon);
registerOnce(ExtensionCategory.EDGE, 'line', Line);
registerOnce(ExtensionCategory.EDGE, 'quadratic', Quadratic);
registerOnce(ExtensionCategory.LAYOUT, 'd3-force', D3ForceLayout);
registerOnce(ExtensionCategory.BEHAVIOR, 'drag-canvas', DragCanvas);
registerOnce(ExtensionCategory.BEHAVIOR, 'zoom-canvas', ZoomCanvas);
registerOnce(ExtensionCategory.BEHAVIOR, 'drag-element', DragElement);
registerOnce(ExtensionCategory.BEHAVIOR, 'click-select', ClickSelect);
registerOnce(ExtensionCategory.BEHAVIOR, 'hover-activate', HoverActivate);
registerOnce(ExtensionCategory.BEHAVIOR, 'auto-adapt-label', AutoAdaptLabel);
registerOnce(ExtensionCategory.TRANSFORM, 'process-parallel-edges', ProcessParallelEdges);
registerOnce(ExtensionCategory.TRANSFORM, 'update-related-edges', UpdateRelatedEdge);
registerOnce(ExtensionCategory.TRANSFORM, 'collapse-expand-node', CollapseExpandNode);
registerOnce(ExtensionCategory.TRANSFORM, 'collapse-expand-combo', CollapseExpandCombo);
registerOnce(ExtensionCategory.TRANSFORM, 'get-edge-actual-ends', GetEdgeActualEnds);
registerOnce(ExtensionCategory.TRANSFORM, 'arrange-draw-order', ArrangeDrawOrder);
registerOnce(ExtensionCategory.THEME, 'light', light);
registerOnce(ExtensionCategory.SHAPE, 'circle', CircleShape);
registerOnce(ExtensionCategory.SHAPE, 'group', Group);
registerOnce(ExtensionCategory.SHAPE, 'line', LineShape);
registerOnce(ExtensionCategory.SHAPE, 'path', Path);
registerOnce(ExtensionCategory.SHAPE, 'text', Text);
registerOnce(ExtensionCategory.SHAPE, 'label', Label);
const download = (url: string, name: string) => {
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click();
  if (url.startsWith('blob:')) URL.revokeObjectURL(url);
};
const safeFile = (value: string) => (value.trim() || 'ontology-graph').replace(/[^\p{L}\p{N}_.-]+/gu, '-').slice(0, 80);
const predicateLabels: Record<string, string> = {
  catalyzes_reported: '催化（文献报告）', has_participant: '参与者', derived_from: '衍生自',
  member_of: '属于', requires_cofactor: '需要辅因子', supports_capability: '支持能力',
};
export const readablePredicate = (predicate: string) => predicateLabels[predicate] || predicate.replace(/_/g, ' ') || '关系待确认';
export const readableLocator = (locator: Record<string, unknown>) => {
  const page = Number(locator.page || 0), start = Number(locator.line_start || 0), end = Number(locator.line_end || 0);
  return [page ? `第 ${page} 页` : '', start ? `第 ${start}${end && end !== start ? `–${end}` : ''} 行` : ''].filter(Boolean).join(' · ');
};

export default function OntologyGraph(props: { workspaceId: string }) {
  let container!: HTMLDivElement;
  let graph: Graph | undefined;
  let renderTimer: ReturnType<typeof setTimeout> | undefined;
  const [data, setData] = createSignal<OntologyGraphData>();
  const [query, setQuery] = createSignal('');
  const [activeQuery, setActiveQuery] = createSignal('');
  const [scope, setScope] = createSignal<'both' | 'public' | 'private'>('both');
  const [ranking, setRanking] = createSignal('coverage');
  const [conflictPair, setConflictPair] = createSignal<{left:string;right:string}>();
  const [issueFilter, setIssueFilter] = createSignal('all');
  let requestVersion = 0;
  const [collapsed, setCollapsed] = createSignal<Set<string>>(new Set());
  const [selected, setSelection] = createSignal<Selection>();
  const [hovered, setHovered] = createSignal<Selection>();
  const selection = createMemo(() => selected() || hovered());
  const [loading, setLoading] = createSignal(false);
  const [error, setError] = createSignal('');
  const [showLabels, setShowLabels] = createSignal(true);
  const [exportScale, setExportScale] = createSignal(2);
  const clusters = createMemo(() => [...new Set((data()?.nodes || []).map(node => node.cluster))].sort());
  const colors = createMemo(() => new Map(clusters().map((item, index) => [item, palette[index % palette.length]])));
  const nodeById = (id: string) => displayData().nodes.find(node => node.id === id);
  const nodeName = (id: string) => data()?.nodes.find(node => node.id === id)?.label || nodeById(id)?.label || '待确认实体';

  const displayData = createMemo(() => {
    const source = data();
    if (!source) return { nodes: [] as DisplayNode[], edges: [] as OntologyGraphEdge[] };
    const folded = collapsed();
    const nodes: DisplayNode[] = [];
    const mapped = new Map<string, string>();
    const grouped = new Map<string, OntologyGraphNode[]>();
    source.nodes.forEach(node => grouped.set(node.cluster, [...(grouped.get(node.cluster) || []), node]));
    grouped.forEach((items, cluster) => {
      if (folded.has(cluster)) {
        const id = `cluster:${cluster}`;
        items.forEach(item => mapped.set(item.id, id));
        nodes.push({ id, label: `${cluster} · ${items.length}`, type: cluster, cluster, scope: items.some(item => item.scope === 'private') ? 'private' : 'public', importance: items.reduce((sum, item) => sum + item.importance, 0), distance: null, aggregate: true, count: items.length });
      } else {
        items.forEach(item => { mapped.set(item.id, item.id); nodes.push(item); });
      }
    });
    // Preserve each assertion's polarity, conditions and provenance, even when folded.
    const edges = source.edges.flatMap(edge => {
      const from = mapped.get(edge.source), to = mapped.get(edge.target);
      return !from || !to || (from === to && edge.source !== edge.target) ? [] : [{ ...edge, original_source: edge.source, original_target: edge.target, source: from, target: to }];
    });
    return { nodes, edges };

  });

  const load = async (text = activeQuery()) => {
    const version = ++requestVersion;
    setLoading(true); setError(''); setSelection(undefined); setHovered(undefined);
    try {
      const params = new URLSearchParams({ text, scope: scope(), ranking: ranking(), limit: '30', depth: '2' });
      const result = await ontologyRequest<OntologyGraphData>(props.workspaceId, `/graph?${params}`);
      if (version !== requestVersion) return;
      setData(result); setActiveQuery(text);
      const counts = new Map<string, number>();
      result.nodes.forEach(node => counts.set(node.cluster, (counts.get(node.cluster) || 0) + 1));
      setCollapsed(new Set([...counts].filter(([, count]) => count > 8 && !text).map(([cluster]) => cluster)));
    } catch (cause) { if (version === requestVersion) setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { if (version === requestVersion) setLoading(false); }
  };

  const render = async () => {
    const shown = displayData();
    graph?.destroy(); graph = undefined;
    if (!container || !shown.nodes.length) return;
    graph = new Graph({
      container,
      autoFit: 'view',
      padding: 44,
      data: {
        nodes: shown.nodes.map(node => ({ id: node.id, data: node })),
        edges: shown.edges.map(edge => ({ id: edge.id, source: edge.source, target: edge.target, data: edge })),
      },
      layout: { type: 'd3-force', link: { distance: shown.nodes.length > 18 ? 220 : 180 }, manyBody: { strength: shown.nodes.length > 18 ? -340 : -240 }, collide: { radius: shown.nodes.length > 18 ? 82 : 68 } },
      node: {
        type: (datum: any) => (datum.data as DisplayNode).aggregate ? 'hexagon' : stableShape((datum.data as DisplayNode).type),
        style: (datum: any) => {
          const node = datum.data as DisplayNode;
          const centered = data()?.center_id === node.id;
          return {
            size: node.aggregate ? 48 : Math.min(44, 20 + Math.log2(1 + (ranking() === 'coverage' ? node.source_count || 0 : node.importance)) * 5),
            fill: node.aggregate ? '#f3f0e5' : (node.scope === 'private' ? '#fff7e8' : '#f5f7ef'),
            stroke: colors().get(node.cluster) || '#65784b', lineWidth: centered ? 4 : 2,
            labelText: showLabels() ? (Array.from(node.label).length > 24 ? Array.from(node.label).slice(0, 24).join('') + '…' : node.label) : '', labelPlacement: 'bottom', labelFontSize: 11,
            labelFill: '#3d4038', labelMaxWidth: 180, labelWordWrap: false,
            labelBackground: true, labelBackgroundFill: '#fffdf8ee', labelBackgroundRadius: 3,
            shadowColor: centered ? '#88976d55' : 'transparent', shadowBlur: centered ? 18 : 0,
          };
        },
      },
      edge: {
        style: (datum: any) => {
          const edge = datum.data as OntologyGraphEdge;
          return { stroke: relationshipColor(edge), lineWidth: edge.conflict ? 2.4 : 1.2, lineDash: edge.scope === 'private' ? [5, 4] : [], endArrow: true, labelText: '', labelFontSize: 11, labelFill: '#73786e', labelBackground: true, labelBackgroundFill: '#fffdf8dd', labelBackgroundRadius: 3 };
        },
      },
      transforms: [{ type: 'process-parallel-edges', mode: 'bundle', distance: 12 }],
      behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element', 'click-select', 'hover-activate', { type: 'auto-adapt-label', padding: 8 }],
      animation: false,
    });
    graph.on('node:click', (event: any) => {
      const node = shown.nodes.find(item => item.id === String(event.target?.id || ''));
      if (!node) return;
      if (node.aggregate) setCollapsed(old => { const next = new Set(old); next.delete(node.cluster); return next; });
      else setSelection({ kind: 'node', value: node });
    });
    graph.on('edge:click', (event: any) => {
      const edge = shown.edges.find(item => item.id === String(event.target?.id || ''));
      if (edge) setSelection({ kind: 'edge', value: edge });
    });
    graph.on('node:pointerenter', (event: any) => {
      const node = shown.nodes.find(item => item.id === String(event.target?.id || ''));
      if (node && !node.aggregate) setHovered({ kind: 'node', value: node });
    });
    graph.on('edge:pointerenter', (event: any) => {
      const edge = shown.edges.find(item => item.id === String(event.target?.id || ''));
      if (edge) setHovered({ kind: 'edge', value: edge });
    });
    graph.on('node:pointerleave', () => setHovered(undefined));
    graph.on('edge:pointerleave', () => setHovered(undefined));
    graph.on('node:dragstart', () => container.classList.add('is-dragging-node'));
    graph.on('node:dragend', () => container.classList.remove('is-dragging-node'));
    graph.on('canvas:click', () => { setSelection(undefined); setHovered(undefined); });
    await graph.render();
  };

  createEffect(() => {
    displayData(); showLabels();
    clearTimeout(renderTimer);
    renderTimer = setTimeout(() => void render(), 0);
  });
  createEffect(on(() => props.workspaceId, () => { setConflictPair(undefined); void load(''); }));
  onMount(() => { const resize = new ResizeObserver(() => graph?.resize()); resize.observe(container); onCleanup(() => resize.disconnect()); });
  onCleanup(() => { requestVersion++; clearTimeout(renderTimer); graph?.destroy(); });

  const toggleCluster = (cluster: string) => setCollapsed(old => { const next = new Set(old); next.has(cluster) ? next.delete(cluster) : next.add(cluster); return next; });
  const includePrivateInExport = async () => !!data()?.has_private
    && await confirmAction('导出默认只包含公共知识。是否明确将当前 Workspace 的私有实体和关系也写入文件？\n\n取消后仍会继续导出，但只包含公共知识。');
  const exportData = async (includePrivate: boolean) => {
    if (includePrivate) return displayData();
    // Recompute in public scope: filtering a mixed graph would leak private
    // source counts and opposing assertion identifiers in otherwise public nodes.
    const params = new URLSearchParams({ text: activeQuery(), scope: 'public', ranking: ranking(), limit: '30', depth: '2' });
    const publicData = await ontologyRequest<OntologyGraphData>(props.workspaceId, `/graph?${params}`);
    return { nodes: publicData.nodes, edges: publicData.edges };
  };
  const exportPng = async () => {
    if (!graph) return;
    const includePrivate = await includePrivateInExport();
    const shown = await exportData(includePrivate);
    if (!shown.nodes.length) throw new Error('当前公共范围没有可导出的实体；如需导出私有知识，请在导出确认中选择包含私有知识。');
    const host = document.createElement('div');
    Object.assign(host.style, { position: 'fixed', left: '-10000px', width: '1400px', height: '1000px' });
    document.body.appendChild(host);
    const exported = new Graph({ container: host, width: 1400, height: 1000, autoFit: 'view', padding: [100, 180, 100, 180],
      data: { nodes: shown.nodes.map(n => ({ id: n.id, data: n })), edges: shown.edges.map(e => ({ id: e.id, source: e.source, target: e.target, data: e })) },
      layout: { type: 'd3-force', link: { distance: 180 }, manyBody: { strength: -240 }, collide: { radius: 68 } },
      node: { style: (d: any) => ({ size: 28, fill: '#f5f7ef', stroke: '#65784b', lineWidth: 2, labelText: d.data.label, labelPlacement: 'bottom', labelFontSize: 12, labelFill: '#30342d', labelMaxWidth: 180, labelWordWrap: true }) },
      transforms: [{ type: 'process-parallel-edges', mode: 'bundle', distance: 12 }],
      edge: { style: (d: any) => ({ stroke: relationshipColor(d.data), lineDash: d.data.scope === 'private' ? [5,4] : [], endArrow: true }) }, animation: false,
    });
    let source: string;
    try { await exported.render(); source = await exported.toDataURL({ mode: 'viewport', type: 'image/png' }); }
    finally { exported.destroy(); host.remove(); }
    const image = new Image();
    image.src = source;
    await image.decode();
    const scale = Math.max(1, Math.min(4, exportScale()));
    const canvas = document.createElement('canvas'); canvas.width = image.width * scale; canvas.height = image.height * scale;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('当前浏览器无法导出图片');
    context.fillStyle = '#fffdf8'; context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    download(canvas.toDataURL('image/png'), `${safeFile(activeQuery())}.png`);
  };
  const exportHtml = async () => {
    const shown = await exportData(await includePrivateInExport());
    if (!shown.nodes.length) throw new Error('当前公共范围没有可导出的实体；如需导出私有知识，请在导出确认中选择包含私有知识。');
    const payload = JSON.stringify({ ...shown, title: activeQuery() || '本体知识梗概', exportedAt: new Date().toISOString() }).replace(/</g, '\\u003c');
    const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>本体知识图谱</title><style>html,body,#graph{height:100%;margin:0}body{font:14px system-ui;background:#fffdf8;color:#252820}header{position:fixed;z-index:2;left:16px;top:12px;max-width:calc(100vw - 64px);max-height:32vh;overflow:auto;overflow-wrap:anywhere;background:#fffdf8e8;padding:10px 14px;border:1px solid #d8d3c7;border-radius:10px}small{display:block;color:#73786e}</style></head><body><header><b></b><small>滚轮缩放 · 拖动画布 · 点击选择</small></header><div id="graph"></div><script type="module">import{Graph}from'https://esm.sh/@antv/g6@5.1.1';const d=${payload};document.querySelector('header b').textContent=d.title;const g=new Graph({container:'graph',autoFit:'view',padding:[110,180,110,180],animation:false,data:{nodes:d.nodes.map(n=>({id:n.id,data:n})),edges:d.edges.map(e=>({id:e.id,source:e.source,target:e.target,data:e}))},layout:{type:'d3-force',link:{distance:180},manyBody:{strength:-240},collide:{radius:68}},node:{style:n=>({size:Math.min(44,20+n.data.importance*2),fill:n.data.scope==='private'?'#fff7e8':'#f5f7ef',stroke:'#65784b',lineWidth:2,labelText:n.data.label,labelPlacement:'bottom',labelFontSize:12,labelFill:'#30342d',labelMaxWidth:180,labelWordWrap:true})},edge:{style:e=>({stroke:(${relationshipColor.toString()})(e.data),lineDash:e.data.scope==='private'?[5,4]:[],endArrow:true,labelText:''})},transforms:[{type:'process-parallel-edges',mode:'bundle',distance:12}],behaviors:['drag-canvas','zoom-canvas','drag-element','click-select','hover-activate']});g.on('edge:click',ev=>{const e=d.edges.find(e=>e.id===String(ev.target?.id));if(e)document.querySelector('header small').textContent=JSON.stringify({polarity:e.polarity,conditions:e.qualifiers,status:e.conflict_categories,source:e.source_id,quote:e.quote})});g.render();</script></body></html>`;
    download(URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' })), `${safeFile(activeQuery())}.html`);
  };

  const runExport = async (operation: () => Promise<void>) => {
    setError('');
    try { await operation(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  };

  return <div class="ontology-graph-shell">
    <div class="ontology-graph-toolbar">
      <form onSubmit={event => { event.preventDefault(); void load(query().trim()); }}><input aria-label="搜索图谱实体" value={query()} onInput={event => setQuery(event.currentTarget.value)} placeholder="搜索实体名称，查看两层关系"/><button type="submit" disabled={loading()}>探索</button><Show when={activeQuery()}><button type="button" onClick={() => { setQuery(''); void load(''); }}>返回梗概</button></Show></form>
      <div class="ontology-graph-actions"><select aria-label="知识范围" value={scope()} onChange={event => { setScope(event.currentTarget.value as any); queueMicrotask(() => void load()); }}><option value="both">公共 + 私有</option><option value="public">仅公共</option><option value="private">仅私有</option></select><select aria-label="重要性排序" value={ranking()} onChange={event => { setRanking(event.currentTarget.value); void load(); }}><option value="coverage">来源覆盖优先</option><option value="connectivity">连接度优先</option></select><label><input type="checkbox" checked={showLabels()} onChange={event => setShowLabels(event.currentTarget.checked)}/> 标签</label><button onClick={() => void runExport(exportPng)}>导出 PNG</button><button onClick={() => void runExport(exportHtml)}>导出交互 HTML</button></div>
    </div>
    <Show when={error()}><p class="ontology-graph-error">{error()}</p></Show>
    <div class="ontology-cluster-strip" aria-label="实体类型图例与折叠控制"><For each={clusters()}>{cluster => {const nodes=()=>data()?.nodes.filter(node=>node.cluster===cluster)||[];const type=()=>nodes()[0]?.type||cluster;return <button classList={{ collapsed: collapsed().has(cluster) }} onClick={() => toggleCluster(cluster)}><i style={{background:colors().get(cluster),'clip-path':shapeClip(stableShape(type()))}}/>{type()}<span>{nodes().length}</span></button>}}</For></div>
    <div class="ontology-graph-help">拖动节点调整位置 · 拖动画布平移 · 滚轮缩放 · 悬停预览，点击固定详情。{ranking() === 'coverage' ? '排序先看已标识来源数，再看相邻实体数、关系数；' : '排序按连接度（关系条数）；'}邻域优先显示近邻。来源多不代表独立验证或结论更可靠。</div>
    <div class="ontology-graph-help">红色：同记录条件下相反 · 蓝色：条件不同 · 金色：条件不足或待核对 · 虚线：私有关系</div>
    <div class="ontology-graph-layout"><div class="ontology-graph-canvas" ref={container}><Show when={loading()}><div class="ontology-graph-loading">正在计算知识关系…</div></Show><Show when={!loading() && !data()?.nodes.length}><div class="ontology-graph-loading">{data()?.status === 'not_found' ? '未找到匹配实体，请更换名称或范围。' : '当前范围还没有可展示的实体关系。'}</div></Show></div><aside class="ontology-graph-detail"><Show when={selection()} fallback={<><strong>实体与关系详情</strong><p>点击实体或关系固定完整信息。虚线表示 Workspace 私有关系，红色表示同记录条件下相反，具体原因可在下方关系列表中查看。</p></>}>{selected => <Show when={selected().kind === 'node'} fallback={(()=>{const edge=selected().value as OntologyGraphEdge;const subject=nodeName(edge.original_source || edge.source),object=nodeName(edge.original_target || edge.target),location=readableLocator(edge.locator||{});return <><strong>{subject} → {readablePredicate(edge.predicate)} → {object}</strong><dl><dt>主体</dt><dd>{subject}</dd><dt>关系</dt><dd>{readablePredicate(edge.predicate)}</dd><dt>客体</dt><dd>{object}</dd><dt>范围</dt><dd>{edge.scope === 'private' ? 'Workspace 私有' : '公共知识'}</dd><dt>结论</dt><dd>{edge.polarity === 'supports' ? '支持' : edge.polarity === 'opposes' ? '反对' : edge.polarity}</dd><dt>比较结果</dt><dd>{conflictText(edge)}</dd><dt>记录条件</dt><dd>{JSON.stringify(edge.qualifiers || {})}</dd><Show when={edge.counterparts?.length}><dt>相反主张</dt><dd><For each={edge.counterparts}>{other => <p>{other.scope === 'private' ? '私有' : '公共'} · {conflictLabels[other.condition_relation]}<br/>{JSON.stringify(other.qualifiers)}<br/>{other.source_id || '来源未标识'}<Show when={other.quote}><blockquote>{other.quote}</blockquote></Show><For each={other.evidence_ids || []}>{id => <button type="button" class="underline" onClick={event => openCitation({kind:'ontology_evidence',id},event.currentTarget)}>查看相反原文</button>}</For></p>}</For></dd></Show><Show when={location}><><dt>原文定位</dt><dd>{location}</dd></></Show><dt>原文证据</dt><dd><Show when={edge.evidence_ids?.length} fallback={'暂无可打开的原文'}><span class="flex flex-wrap gap-1">{edge.evidence_ids.map(id => <button type="button" class="underline decoration-dotted" onClick={event => openCitation({kind:'ontology_evidence',id},event.currentTarget)}>查看原文</button>)}</span></Show></dd></dl><Show when={edge.quote}><blockquote>{edge.quote}</blockquote></Show><details><summary>技术详情（可复制）</summary><pre>{JSON.stringify({relation_id:edge.id,subject_id:edge.source,predicate:edge.predicate,object_id:edge.target,evidence_ids:edge.evidence_ids,source_id:edge.source_id,source_revision:edge.source_revision,locator:edge.locator},null,2)}</pre></details></>})()}>{(()=>{const node=selected().value as DisplayNode;return <><strong>{node.label || '待确认实体'}</strong><dl><dt>类型</dt><dd>{node.type}</dd><Show when={node.description}><><dt>说明</dt><dd>{node.description}</dd></></Show><dt>分组</dt><dd>{node.cluster}</dd><dt>已标识来源</dt><dd>{node.source_count ?? '—'}</dd><dt>相邻实体</dt><dd>{node.neighbor_count ?? '—'}</dd><dt>关联</dt><dd>{node.importance} 条关系</dd><dt>身份范围</dt><dd>{node.scope === 'private' ? 'Workspace 私有' : '公共实体'}</dd></dl><details><summary>技术详情（可复制）</summary><pre>{JSON.stringify({entity_id:node.id,center_distance:node.distance},null,2)}</pre></details></>})()}</Show>}</Show></aside></div>
    <details class="ontology-graph-issues"><summary>关系核对 · {(data()?.edges || []).filter(edge => edge.conflict_categories?.length || edge.conflict).length} 条待比较关系</summary>
      <label>筛选 <select aria-label="关系核对筛选" value={issueFilter()} onChange={event => setIssueFilter(event.currentTarget.value)}><option value="all">全部差异与争议</option><For each={Object.entries(conflictLabels)}>{([key,label]) => <option value={key}>{label}</option>}</For></select></label>
      <For each={(data()?.edges || []).filter(edge => issueFilter() === 'all' ? edge.conflict_categories?.length || edge.conflict : edge.conflict_categories?.includes(issueFilter())).slice(0, 50)}>{edge => <button type="button" onClick={() => { setSelection({kind:'edge',value:edge}); setHovered(undefined); }}>
        {data()?.nodes.find(node => node.id === edge.source)?.label} → {readablePredicate(edge.predicate)} → {data()?.nodes.find(node => node.id === edge.target)?.label} · {edge.polarity === 'opposes' ? '反对' : '支持'} · {conflictText(edge)}
      </button>}</For>
      <p>列表最多展示前 50 条，请按类型筛选或搜索实体缩小范围。比较仅覆盖当前知识范围；条件相同指已记录字段相同，不代表完整实验条件相同。</p>
    </details>
    <Show when={selected()?.kind === 'edge' && (selected()?.value as OntologyGraphEdge)?.counterparts?.length}><div class="ontology-conflict-entry"><strong>处理选中的相反主张</strong><For each={(selected()?.value as OntologyGraphEdge)?.counterparts || []}>{other => <button type="button" onClick={() => setConflictPair({left:selected()!.value.id,right:other.id})}>{other.source_id || '未标识来源'} · {conflictLabels[other.condition_relation]} · 查看与处理</button>}</For></div></Show>
    <Show when={conflictPair()}>{pair => <OntologyConflicts workspaceId={props.workspaceId} leftId={pair().left} rightId={pair().right} onChanged={() => void load()} onClose={() => setConflictPair(undefined)}/>}</Show>
    <footer class="ontology-graph-footer"><span>{data()?.nodes.length || 0} 个实体 · {data()?.edges.length || 0} 条关系</span><label>PNG 清晰度 <input type="range" min="1" max="4" step="1" value={exportScale()} onInput={event => setExportScale(Number(event.currentTarget.value))}/>{exportScale()}×</label><span role="status">{graphCompletenessText(data())}</span></footer>
  </div>;
}
