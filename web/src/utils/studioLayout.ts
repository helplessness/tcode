export const studioPages = [
  { id: 'console', label: '当前任务', group: '任务', mark: '任' },
  { id: 'jobs', label: '远程计算', group: '任务', mark: '算' },
  { id: 'review', label: '结果审查', group: '任务', mark: '审' },
  { id: 'subagents', label: '协作', group: '任务', mark: '协' },
  { id: 'structure', label: '三维结构', group: '任务', mark: '构' },
  { id: 'protein-runs', label: '实验运行', group: '任务', mark: '验' },
  { id: 'assets', label: '文件', group: '任务', mark: '文' },
  { id: 'changes', label: '任务改动', group: '任务', mark: '改' },
  { id: 'symphony', label: '运行总览', group: '任务', mark: '总' },
  { id: 'sidechat', label: '旁支聊天', group: '任务', mark: '支' },
  { id: 'team', label: '团队协作', group: '任务', mark: '队' },
  { id: 'research', label: '研究中心', group: '研究库', mark: '研' },
  { id: 'library', label: '资料库', group: '研究库', mark: '资' },
  { id: 'wiki', label: '文献 Wiki', group: '研究库', mark: '文' },
  { id: 'ontology', label: '本体知识', group: '研究库', mark: '知' },
  { id: 'scheduled', label: '定时任务', group: '自动化', mark: '时' },
  { id: 'runtime-workflow', label: '运行时 Workflow', group: '自动化', mark: '临' },
  { id: 'workflow', label: '研究流程', group: '自动化', mark: '流' },
  { id: 'kanban', label: '任务看板', group: '自动化', mark: '板' },
  { id: 'mcp', label: 'MCP 服务', group: '扩展', mark: '连' },
  { id: 'plugins', label: '插件', group: '扩展', mark: '插' },
  { id: 'settings', label: '设置', group: '设置', mark: '设' },
] as const;
export type StudioPage = typeof studioPages[number]['id'];
export type StudioSurface = 'web' | 'desktop';
// Contextual pages remain deep-linkable and open as task tabs, but do not occupy fixed navigation.
const contextualPageIds: StudioPage[] = ['console', 'sidechat', 'team', 'protein-runs'];
export const studioNavigationPages = studioPages.filter(page => !contextualPageIds.includes(page.id));
export const isPageAvailableOnSurface = (page: StudioPage, _surface: StudioSurface) => isStudioPage(page);
export const studioNavigationPagesForSurface = (surface: StudioSurface) => studioNavigationPages.filter(page => isPageAvailableOnSurface(page.id, surface));
export const isCollaborationPage = (page: StudioPage) => ['subagents', 'team', 'sidechat'].includes(page);
export const isStudioPage = (value: string): value is StudioPage => studioPages.some(page => page.id === value);
export const studioPageFromHash = (hash: string): StudioPage => {
  const value = hash.replace(/^#\/?/, '').split('?')[0];
  return isStudioPage(value) ? value : 'console';
};
export const studioPanels = { rail:'活动导航', tree:'资源与会话', inspector:'协作面板', dock:'执行区', tabs:'工作标签' };
export type StudioPanel = keyof typeof studioPanels;
export type ResizablePanel = 'tree' | 'inspector' | 'dock';
export interface StudioLayout {
  inspectorMerged: boolean;
  navigationShare: number;
  sizes: Record<ResizablePanel,number>;
  hidden: Record<StudioPanel,boolean>;
}
export const defaultStudioLayout = (): StudioLayout => ({
  inspectorMerged: false,
  navigationShare: .22,
  sizes: {tree:.205,inspector:.19,dock:.21},
  hidden: {rail:false,tree:false,inspector:false,dock:false,tabs:false},
});
export function parseStudioLayout(raw: string | null): StudioLayout {
  const value=defaultStudioLayout();
  try {
    const saved=JSON.parse(raw || '{}');
    value.inspectorMerged=saved?.inspectorMerged===true;
    if (typeof saved.navigationShare === 'number' && Number.isFinite(saved.navigationShare)) value.navigationShare=Math.max(.10,Math.min(.55,saved.navigationShare));
    for (const id of ['tree','inspector','dock'] as const) {
      if (typeof saved?.sizes?.[id]==='number' && Number.isFinite(saved.sizes[id])) value.sizes[id]=Math.max(.08,Math.min(id==='inspector'?1:.5,saved.sizes[id]));
    }
    for (const id of Object.keys(studioPanels) as StudioPanel[]) value.hidden[id]=saved?.hidden?.[id]===true;
  } catch { /* A stale preference must not prevent startup. */ }
  return value;
}

export function inspectorAnchors(width: number, rail: number) {
  const max = Math.max(0, width - rail), min = Math.min(280, max);
  // The first point is a lower bound, not a snap target in the free-resize zone.
  return [...new Set([min, max])];
}
export function snapInspector(width: number, _velocity: number, anchors: number[]) {
  const max=anchors[anchors.length-1];
  return width>max*.75 ? max : Math.max(anchors[0],width);
}
export function rubberWidth(width: number, min: number, max: number) {
  const bound = Math.max(min, Math.min(max, width)), excess = width - bound;
  return bound + Math.sign(excess) * 64 * (1 - Math.exp(-Math.abs(excess) / 180));
}
export function drawerSpringStep(width: number, velocity: number, target: number, dt: number) {
  const step = Math.min(32, Math.max(0, dt));
  const speed = (velocity + (target - width) * .00032 * step) * Math.exp(-.024 * step);
  return { width: width + speed * step, velocity: speed };
}
export function studioDimensions(layout: StudioLayout, width: number, height: number, focus=false, railWidth=52, mainMinimum=360) {
  const hidden=(id: StudioPanel)=>focus || layout.hidden[id];
  const rail=hidden('rail')?0:railWidth;
  const treeMinimum=railWidth===64?180:200;
  let tree=hidden('tree')?0:Math.max(treeMinimum,Math.min(460,width*layout.sizes.tree));
  let inspector=hidden('inspector')?0:Math.max(280,Math.min(width*.6,width*layout.sizes.inspector));
  const excess=Math.max(0,rail+tree+inspector+mainMinimum-width);
  const treeReduction=Math.min(excess,Math.max(0,tree-180));
  tree-=treeReduction;inspector=Math.max(0,inspector-(excess-treeReduction));
  // On small screens panes become stacked; no invisible minimum width leaks out.
  if(width<=780){tree=width;inspector=width;}
  const dock=hidden('dock')?0:Math.max(100,Math.min(height*.5,height-280,height*layout.sizes.dock));
  return {rail,tree,inspector,dock};
}
