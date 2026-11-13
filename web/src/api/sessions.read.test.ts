import { afterEach, expect, it, vi } from 'vitest';
import { markAllAsyncCompletionsRead } from './sessions';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it('times out a stalled read-all request and permits retry', async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  vi.stubGlobal('fetch', fetchMock);
  const pending = expect(markAllAsyncCompletionsRead()).rejects.toThrow('标记已读超时');
  await vi.advanceTimersByTimeAsync(10_000);
  await pending;
  fetchMock.mockImplementationOnce(async () => ({ ok: true }) as any);
  await markAllAsyncCompletionsRead();
  expect(vi.getTimerCount()).toBe(0);
});
