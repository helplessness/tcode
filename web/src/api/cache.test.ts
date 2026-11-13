import { beforeEach, describe, expect, it, vi } from 'vitest';

import { resetShortLivedGetCache, withShortLivedGetCache } from './cache';

describe('short-lived GET cache', () => {
  beforeEach(() => resetShortLivedGetCache());

  it('deduplicates concurrent reads', async () => {
    const loader = vi.fn(async () => 'session-a');

    const [first, second] = await Promise.all([
      withShortLivedGetCache('session:a', 800, loader),
      withShortLivedGetCache('session:a', 800, loader),
    ]);

    expect([first, second]).toEqual(['session-a', 'session-a']);
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it('does not let a response from the previous identity refill the cache', async () => {
    let resolveOld!: (value: string) => void;
    const oldRequest = withShortLivedGetCache(
      'session:shared-id',
      800,
      () => new Promise<string>((resolve) => { resolveOld = resolve; }),
    );

    resetShortLivedGetCache();
    const currentRequest = withShortLivedGetCache('session:shared-id', 800, async () => 'current-user');
    resolveOld('previous-user');

    expect(await oldRequest).toBe('previous-user');
    expect(await currentRequest).toBe('current-user');
    expect(await withShortLivedGetCache('session:shared-id', 800, async () => 'unexpected')).toBe('current-user');
  });
});
