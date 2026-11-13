import { afterEach, expect, it, vi } from 'vitest';
import { getHumanReviewQueue } from './humanReview';

afterEach(() => vi.unstubAllGlobals());

it('targets a review using only a read request, without approving or resuming', async () => {
  const review = { id: 'r-1', identifier: 'REVIEW-1', status: 'pending_review' };
  const fetcher = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({ reviews: [review], count: 1, total: 1 })));
  vi.stubGlobal('fetch', fetcher);
  expect((await getHumanReviewQueue('', 1, { reviewId: ' r-1 ', reviewIdentifier: ' REVIEW-1 ' })).reviews).toEqual([review]);
  expect(fetcher).toHaveBeenCalledTimes(1);
  const url = new URL(fetcher.mock.calls[0][0] as string, 'http://localhost');
  expect(url.pathname).toBe('/api/human-review/queue');
  expect(url.searchParams.get('review_id')).toBe('r-1');
  expect(url.searchParams.get('review_identifier')).toBe('REVIEW-1');
  expect(url.searchParams.get('status')).toBe('');
  expect(fetcher.mock.calls[0]).toHaveLength(1); // fetch defaults to GET.
});

it('preserves the existing queue request when no target is supplied', async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({ reviews: [] })));
  vi.stubGlobal('fetch', fetcher);
  await getHumanReviewQueue();
  expect(fetcher.mock.calls[0][0]).toBe('/api/human-review/queue?status=pending_review&limit=50');
});

it('passes session scope before server pagination and reports fetch failures instead of zero', async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({detail:'不可访问'}), {status:403}));
  vi.stubGlobal('fetch', fetcher);
  await expect(getHumanReviewQueue('', 200, {sessionId:' session-1 '})).rejects.toThrow('不可访问');
  expect(new URL(fetcher.mock.calls[0][0] as string, 'http://localhost').searchParams.get('session_id')).toBe('session-1');
  vi.stubGlobal('fetch', async () => { throw new Error('offline'); });
  await expect(getHumanReviewQueue()).rejects.toThrow('offline');
  vi.stubGlobal('fetch', async () => new Response('not json'));
  await expect(getHumanReviewQueue()).rejects.toThrow('审核记录响应无效');
});
