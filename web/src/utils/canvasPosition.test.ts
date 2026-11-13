import { describe, expect, it } from 'vitest';
import { canvasPosition, canvasTranslation } from './canvasPosition';

describe('restricted canvas offsets', () => {
  it('keeps existing offsets and handles one-axis translations', () => {
    expect(canvasPosition()).toEqual([0, 0]);
    expect(canvasPosition('translate(20, -3.5)')).toEqual([20, -3.5]);
    expect(canvasPosition('translate(1e2)')).toEqual([100, 0]);
    expect(canvasTranslation('20', '-3.5')).toBe('translate(20 -3.5)');
  });
  it('rejects empty, non-finite, out-of-range and unsupported transforms', () => {
    for (const value of ['', 'NaN', 'Infinity', '100001']) expect(canvasTranslation(value, '0')).toBeNull();
    for (const value of ['rotate(20)', 'translate(1) scale(2)', 'translate(1e999)', 'translate(100001)', 'translate(--)'])
      expect(canvasPosition(value)).toBeNull();
  });
});
