import { afterEach, expect, it, vi } from 'vitest';
import { passVerticalScrollToParent } from './scrollBoundary';

afterEach(() => vi.unstubAllGlobals());

it('hands off at both edges, preserves nested scrolling, zoom and wheel units', () => {
  const outer = { parentElement: null, scrollTop: 100, clientHeight: 200, scrollHeight: 900, scrollBy: vi.fn() };
  const viewport = { parentElement: outer, scrollTop: 0, clientHeight: 100, scrollHeight: 300 };
  const inner = { parentElement: viewport, scrollTop: 40, clientHeight: 50, scrollHeight: 200 };
  vi.stubGlobal('window', { getComputedStyle: () => ({ overflowY: 'auto' }) });
  const wheel = (changes = {}) => ({ target: viewport, currentTarget: viewport, deltaY: -20, deltaX: 0, deltaMode: 0,
    ctrlKey: false, metaKey: false, defaultPrevented: false, preventDefault: vi.fn(), ...changes } as unknown as WheelEvent);
  const up = wheel(); passVerticalScrollToParent(up);
  expect(up.preventDefault).toHaveBeenCalled();
  expect(outer.scrollBy).toHaveBeenLastCalledWith({ top: -20, behavior: 'instant' });
  outer.scrollBy.mockClear();
  passVerticalScrollToParent(wheel({ target: inner }));
  passVerticalScrollToParent(wheel({ ctrlKey: true }));
  passVerticalScrollToParent(wheel({ deltaX: 40 }));
  viewport.scrollTop = 100; passVerticalScrollToParent(wheel());
  expect(outer.scrollBy).not.toHaveBeenCalled();
  viewport.scrollTop = 200; passVerticalScrollToParent(wheel({ deltaY: 2, deltaMode: 1 }));
  expect(outer.scrollBy).toHaveBeenLastCalledWith({ top: 32, behavior: 'instant' });
  outer.scrollTop = 700; outer.scrollBy.mockClear();
  const bottom = wheel({ deltaY: 20 }); passVerticalScrollToParent(bottom);
  expect(bottom.preventDefault).not.toHaveBeenCalled();
  expect(outer.scrollBy).not.toHaveBeenCalled();
});
