import { describe, expect, it } from 'vitest';

import { getToolTextDisplayStats } from './toolTextDisplay';

describe('tool text display stats', () => {
  it('counts lines and characters for compact tool output labels', () => {
    expect(getToolTextDisplayStats('a\nbc')).toMatchObject({
      lineCount: 2,
      charCount: 4,
      isLong: false,
      label: '2 行 / 4 字符',
    });
  });

  it('marks very long output for constrained rendering', () => {
    const text = Array.from({ length: 25 }, (_, index) => `line ${index}`).join('\n');
    expect(getToolTextDisplayStats(text).isLong).toBe(true);
  });
});
