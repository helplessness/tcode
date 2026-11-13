import { describe, expect, it } from 'vitest';
import type { SubagentItem } from '../api/subagents';
import { buildSubagentTree, summarizeSubagentTree } from './subagentTree';

const item = (session_id: string, parent_session_id: string | undefined, status: string, created_at: number): SubagentItem => ({
  session_id, parent_session_id, status, created_at, agent_name: session_id, title: session_id, message_count: 0, last_message: '',
});

describe('subagentTree', () => {
  it('builds nested delegation branches and keeps orphaned sessions visible', () => {
    const tree = buildSubagentTree([
      item('child', 'parent', 'running', 2),
      item('orphan', 'missing', 'idle', 3),
      item('parent', 'root-session', 'completed', 1),
    ]);

    expect(tree.map(node => node.item.session_id)).toEqual(['parent', 'orphan']);
    expect(tree[0]?.children[0]?.item.session_id).toBe('child');
  });

  it('normalizes runtime statuses into overview counters', () => {
    const items = [item('a', undefined, 'busy', 1), item('b', undefined, 'done', 2), item('c', undefined, 'interrupted', 3)];
    expect(summarizeSubagentTree(items, entry => entry.status)).toEqual({ running: 1, idle: 0, completed: 1, failed: 1 });
  });
});

