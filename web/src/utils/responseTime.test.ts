import { expect, it } from 'vitest';
import { formatResponseTime } from './responseTime';

it('formats today, other days and other years using Beijing dates', () => {
  const now = Date.parse('2026-10-10T08:00:00+08:00');
  expect(formatResponseTime(Date.parse('2026-10-10T15:14:00+08:00'), now)).toBe('15:14');
  expect(formatResponseTime(Date.parse('2026-10-08T15:14:00+08:00'), now)).toBe('10月8日 15:14');
  expect(formatResponseTime(Date.parse('2025-10-08T15:14:00+08:00'), now)).toBe('2025年10月8日 15:14');
  expect(formatResponseTime(Date.parse('2026-10-09T16:14:00Z'), now)).toBe('00:14');
  expect(formatResponseTime(Date.parse('2025-12-31T16:14:00Z'), Date.parse('2026-01-01T08:00:00+08:00'))).toBe('00:14');
  expect(formatResponseTime(0, now)).toBe('');
  expect(formatResponseTime(NaN, now)).toBe('');
});
