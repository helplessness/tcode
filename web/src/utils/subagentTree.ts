import type { SubagentItem } from '../api/subagents';

export type SubagentTreeNode = {
  item: SubagentItem;
  children: SubagentTreeNode[];
};

export type SubagentTreeStats = {
  running: number;
  idle: number;
  completed: number;
  failed: number;
};

export const normalizeSubagentStatus = (status?: string) => {
  const value = String(status || '').trim().toLowerCase();
  if (value === 'running' || value === 'busy') return 'running';
  if (value === 'completed' || value === 'done') return 'completed';
  if (['failed','error','terminated','interrupted','partial','cancelled','blocked'].includes(value)) return 'failed';
  return 'idle';
};

export function buildSubagentTree(items: SubagentItem[]): SubagentTreeNode[] {
  const nodes = new Map(items.map(item => [item.session_id, { item, children: [] } satisfies SubagentTreeNode]));
  const roots: SubagentTreeNode[] = [];

  for (const node of nodes.values()) {
    const parentId = String(node.item.parent_session_id || '').trim();
    const parent = parentId && parentId !== node.item.session_id ? nodes.get(parentId) : undefined;
    (parent?.children || roots).push(node);
  }

  const sortNodes = (entries: SubagentTreeNode[]) => {
    entries.sort((a, b) => a.item.created_at - b.item.created_at || a.item.session_id.localeCompare(b.item.session_id));
    entries.forEach(entry => sortNodes(entry.children));
  };
  sortNodes(roots);
  return roots;
}

export function summarizeSubagentTree(items: SubagentItem[], statusOf: (item: SubagentItem) => string): SubagentTreeStats {
  const stats: SubagentTreeStats = { running: 0, idle: 0, completed: 0, failed: 0 };
  for (const item of items) stats[normalizeSubagentStatus(statusOf(item))] += 1;
  return stats;
}

