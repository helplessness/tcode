import { afterEach, expect, it, vi } from 'vitest';
import { openCardDetail } from './cardTransition';

afterEach(() => vi.unstubAllGlobals());

it('uses native snapshots only on desktop and cleans names after completion', async () => {
  const source = { style: { viewTransitionName: '' }, dataset: {} } as HTMLElement;
  const target = { style: { viewTransitionName: '' }, dataset: {} } as HTMLElement;
  const open = vi.fn();
  const start = vi.fn((update: () => void) => {
    expect(source.style.viewTransitionName).toBe('tcode-card-detail');
    update();
    expect(target.style.viewTransitionName).toBe('tcode-card-detail');
    return { finished: Promise.resolve() };
  });
  const root = { dataset: { desktop: 'true' }, classList: { add: vi.fn(), remove: vi.fn() } };
  vi.stubGlobal('document', { documentElement: root, querySelector: (selector: string) => selector === '.detail' ? target : null, startViewTransition: start });
  vi.stubGlobal('window', { matchMedia: () => ({ matches: false }) });
  openCardDetail(source, '.detail', open);
  await Promise.resolve();
  await Promise.resolve();
  expect(open).toHaveBeenCalledTimes(1);
  expect(source.style.viewTransitionName).toBe('');
  expect(target.style.viewTransitionName).toBe('');
  root.dataset.desktop = 'false';
  openCardDetail(source, '.detail', open);
  root.dataset.desktop = 'true';
  vi.stubGlobal('window', { matchMedia: () => ({ matches: true }) });
  openCardDetail(source, '.detail', open);
  expect(start).toHaveBeenCalledTimes(1);
  expect(open).toHaveBeenCalledTimes(3);
});
