import { expect, it } from 'vitest';
import { tabIndicatorFrames } from './TabIndicator';
it('stretches to twice the width, overshoots once and settles without scale', () => {
  const frames = tabIndicatorFrames({ left: 0, width: 80 }, { left: 100, width: 90 }, 400);
  expect(frames[1].width).toBe('180px');
  expect(frames[2].left).toBe('104px');
  expect(frames[3]).toEqual({ left: '100px', width: '90px', offset: 1 });
  expect(frames.every(f => !('transform' in f))).toBe(true);
  const reverse = tabIndicatorFrames({ left: 200, width: 80 }, { left: 50, width: 80 }, 400);
  expect(reverse[2].left).toBe('46px');
  const edge = tabIndicatorFrames({ left: 0, width: 90 }, { left: 100, width: 90 }, 190);
  expect(edge.every(f => parseFloat(f.left) >= 0 && parseFloat(f.left) + parseFloat(f.width) <= 190)).toBe(true);
});
