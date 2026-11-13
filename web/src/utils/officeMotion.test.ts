import { expect, it } from 'vitest';
import { clampOfficeZoom, officeActorPalette, officeActorState, officeToolGlyph, reportLines, shouldReport } from './officeMotion';

it('reports a new completion once, never historical, failed, or idle tasks', () => {
  expect(shouldReport(undefined, 'completed')).toBe(false);
  expect(shouldReport('busy', 'done')).toBe(true);
  expect(shouldReport('completed', 'completed')).toBe(false);
  expect(shouldReport('running', 'failed')).toBe(false);
  expect(shouldReport('running', 'idle')).toBe(false);
  expect(shouldReport('completed', 'running')).toBe(false);
});

it('keeps office zoom usable at both limits', () => {
  expect(clampOfficeZoom(0.01)).toBe(0.25);
  expect(clampOfficeZoom(1.5)).toBe(1.5);
  expect(clampOfficeZoom(10)).toBe(4);
});

it('offers several short report lines for the office visit', () => {
  expect(reportLines.length).toBeGreaterThanOrEqual(6);
  expect(new Set(reportLines).size).toBe(reportLines.length);
  expect(reportLines.every(line => line.length <= 12)).toBe(true);
});

it('derives useful visual states and tool glyphs from live work data', () => {
  expect(officeActorState('running', '正在思考')).toBe('thinking');
  expect(officeActorState('running', '执行工具', 'web_search')).toBe('tooling');
  expect(officeActorState('running', '等待批准')).toBe('asking');
  expect(officeActorState('interrupted')).toBe('interrupted');
  expect(officeToolGlyph('write_file')).toBe('✎');
  expect(officeToolGlyph('web_search')).toBe('⌕');
});

it('keeps an agent palette stable by session id', () => {
  expect(officeActorPalette('agent-a')).toEqual(officeActorPalette('agent-a'));
  expect(officeActorPalette('agent-a')).not.toEqual(officeActorPalette('agent-b'));
});
