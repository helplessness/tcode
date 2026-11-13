import { describe, expect, it } from 'vitest';
import { layoutWorkflowDag } from './workflowDag';

describe('layoutWorkflowDag', () => {
  it('places dependencies before downstream and preserves branches', () => {
    const graph = layoutWorkflowDag([
      { stage_id: 'plan' },
      { stage_id: 'a', depends_on: ['plan'] },
      { stage_id: 'b', depends_on: ['plan'] },
      { stage_id: 'report', depends_on: ['a', 'b'] },
    ]);
    expect(graph.nodes.map(node => [node.stage_id, node.layer])).toEqual([
      ['plan', 0], ['a', 1], ['b', 1], ['report', 2],
    ]);
    expect(graph.edges).toHaveLength(4);
  });
});
