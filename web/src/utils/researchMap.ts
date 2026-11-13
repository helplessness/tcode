import type { ResearchExperiment } from '../api/research';
export type MapPoint = { id: string; parent: string; kind: 'project' | 'session' | 'experiment' | 'group' | 'hypothesis' | 'finding' | 'conflict' };
export function researchMapAncestors(nodes: MapPoint[], id: string) {
  const byId = new Map(nodes.map(n => [n.id, n]));
  const ancestors = new Set<string>();
  let parent = byId.get(id)?.parent;
  while (parent && byId.has(parent) && !ancestors.has(parent)) {
    ancestors.add(parent);
    parent = byId.get(parent)?.parent;
  }
  return ancestors;
}
// 统计整条下游分支，而不只是直接子节点；不包含父节点自身。
export function researchMapDescendantCounts(nodes: MapPoint[]) {
  const byId = new Map(nodes.map(n => [n.id, n]));
  const counts = new Map(nodes.map(n => [n.id, 0]));
  for (const node of nodes) {
    const seen = new Set([node.id]);
    let parent = node.parent;
    while (parent && byId.has(parent) && !seen.has(parent)) {
      seen.add(parent);
      counts.set(parent, counts.get(parent)! + 1);
      parent = byId.get(parent)!.parent;
    }
  }
  return counts;
}
export function researchMapLayout(nodes: MapPoint[], expanded: Set<string>, analysis = false) {
  const points = new Map<string, { x: number; y: number; visible: boolean }>();
  const root = nodes.find(n => !n.parent);
  if (!root) return { width: 1100, height: 680, points };
  const sources = nodes.filter(n => n.parent === root.id);
  const focused = analysis || expanded.has(root.id) && sources.some(n => expanded.has(n.id));
  const columnGap = analysis ? 320 : 220, rowGap = analysis ? 200 : 140;
  let row = 0, maxDepth = 1;
  const place = (node: MapPoint, depth: number, visible: boolean) => {
    if (visible) maxDepth = Math.max(maxDepth, depth);
    const children = nodes.filter(n => n.parent === node.id), start = row;
    children.forEach(child => place(child, depth + 1, visible && expanded.has(node.id)));
    if (visible && (!children.length || !expanded.has(node.id))) row++;
    points.set(node.id, { x: 130 + depth * columnGap, y: 80 + (start + Math.max(start, row - 1)) / 2 * rowGap, visible });
  };
  sources.forEach(source => place(source, 1, expanded.has(root.id)));
  const height = focused ? Math.max(680, row * rowGap + 120) : Math.max(680, Math.ceil(sources.length / 8) * 260 + 300);
  const width = focused ? Math.max(1100, maxDepth * columnGap + 280) : Math.max(1100, Math.ceil(sources.length / 8) * 240 + 600);
  points.set(root.id, { x: focused ? 130 : width / 2, y: height / 2, visible: true });
  if (!focused) sources.forEach((source, i) => {
    const ring = Math.floor(i / 8), count = Math.min(8, sources.length - ring * 8);
    const angle = -Math.PI / 2 + (i % 8) * Math.PI * 2 / count;
    points.set(source.id, { x: width / 2 + Math.cos(angle) * (240 + ring * 120), y: height / 2 + Math.sin(angle) * (205 + ring * 120), visible: expanded.has(root.id) });
  });
  // Parent-first input keeps hidden descendants at their visible ancestor.
  nodes.forEach(node => {
    if (points.get(node.id) && !points.get(node.id)!.visible && node.parent) points.set(node.id, { ...points.get(node.parent)!, visible: false });
  });
  return { width, height, points };
}
export function experimentParents(rows: ResearchExperiment[]) {
  const raw = new Map(rows.map(e => [e.experiment_id, e.parent_experiment_id || '']));
  return new Map(rows.map(e => {
    let cursor = raw.get(e.experiment_id) || '';
    const seen = new Set([e.experiment_id]);
    while (cursor && raw.has(cursor)) {
      if (seen.has(cursor)) return [e.experiment_id, ''];
      seen.add(cursor); cursor = raw.get(cursor) || '';
    }
    const parent = raw.get(e.experiment_id) || '';
    return [e.experiment_id, raw.has(parent) ? parent : ''];
  }));
}
