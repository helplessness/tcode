import { describe, expect, it } from 'vitest';
import { sanitizeMessageContentBlocks } from './contentBlocks';
import { canvasSelectionRequest } from './canvasSelection';

describe('canvas context boundary', () => {
  it('preserves exact selection/version and treats text as data', () => {
    const request = canvasSelectionRequest('canvas-1', 'revision-3', 'wheel', '把这里改蓝\n"x"', 'selection-1');
    const data = JSON.parse(request.slice(request.indexOf('\n') + 1));
    expect(data).toEqual({ canvas_id: 'canvas-1', base_revision_id: 'revision-3', selection_id: 'selection-1', element_ids: ['wheel'], request: '把这里改蓝\n"x"' });
  });
  it('accepts only versioned canvas references and discards extra source/token fields', () => {
    const block = { type: 'canvas', schema: 'tcode.canvas.v1', kind: 'svg', canvas_id: 'canvas_' + 'a'.repeat(32),
      revision_id: 'rev_' + 'b'.repeat(32), session_id: 's', title: '图', source: '<script/>', token: 'private' };
    const result = sanitizeMessageContentBlocks([block, block]);
    expect(result).toHaveLength(1);
    expect(result[0]).not.toHaveProperty('source');
    expect(result[0]).not.toHaveProperty('token');
    expect(sanitizeMessageContentBlocks([{ ...block, revision_id: '../escape' }])).toEqual([]);
  });
});
