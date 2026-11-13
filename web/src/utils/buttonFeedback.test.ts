import { describe, expect, it } from 'vitest';
import { rippleGeometry } from './buttonFeedback';

describe('rippleGeometry', () => {
  it('covers every corner from the pointer origin', () => {
    for (const [x, y] of [[0, 0], [100, 40], [20, 15], [50, 20]]) {
      const wave = rippleGeometry(100, 40, x, y);
      for (const [cx, cy] of [[0, 0], [100, 0], [0, 40], [100, 40]]) {
        expect(wave.diameter / 2).toBeGreaterThanOrEqual(Math.hypot(cx - x, cy - y));
      }
    }
  });
});
