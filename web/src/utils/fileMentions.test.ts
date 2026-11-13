import { describe, expect, it } from 'vitest';

import { findFileMentionTrigger, removeFileMentionTrigger } from './fileMentions';

describe('workspace file mentions', () => {
  it('finds a file query at the start or after whitespace', () => {
    expect(findFileMentionTrigger('@paper', 6)).toEqual({
      query: 'paper',
      start: 0,
      end: 6,
    });
    expect(findFileMentionTrigger('请读取 @docs/paper', 16)).toEqual({
      query: 'docs/paper',
      start: 4,
      end: 15,
    });
  });

  it('does not treat email addresses or an old token as a live trigger', () => {
    expect(findFileMentionTrigger('user@example.com')).toBeNull();
    expect(findFileMentionTrigger('@paper 后续文字')).toBeNull();
  });

  it('removes the trigger while preserving surrounding text', () => {
    const trigger = findFileMentionTrigger('分析 @paper 后续', 9);
    expect(trigger).not.toBeNull();
    expect(removeFileMentionTrigger('分析 @paper 后续', trigger!)).toEqual({
      value: '分析  后续',
      cursor: 3,
    });
  });
});
