import { describe, expect, it } from 'vitest';
import {
  fuzzyHighlightSegments,
  fuzzyMatchIndexes,
  fuzzyMatchesFields,
} from './fuzzySearch';

describe('fuzzy search helpers', () => {
  it('优先匹配连续文本，并忽略大小写', () => {
    expect(fuzzyMatchIndexes('A123V improves activity', 'a123v')).toEqual([0, 1, 2, 3, 4]);
  });

  it('支持按字符顺序进行模糊匹配', () => {
    expect(fuzzyMatchIndexes('A123V improves activity', 'A13V')).toEqual([0, 1, 3, 4]);
    expect(fuzzyMatchesFields(['活性提高了 20%', 'paper:fact:7'], '活高 事实')).toBe(false);
    expect(fuzzyMatchesFields(['活性提高了 20%', 'paper:fact:7'], '活高 fact7')).toBe(true);
  });

  it('只标记实际命中的字符', () => {
    expect(fuzzyHighlightSegments('A123V', 'A13V')).toEqual([
      { text: 'A1', highlighted: true },
      { text: '2', highlighted: false },
      { text: '3V', highlighted: true },
    ]);
  });
});
