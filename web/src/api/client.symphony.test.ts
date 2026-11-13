import { afterEach, describe, expect, it, vi } from 'vitest';

import { batchDeleteSymphonyLocalIssues } from './client';

describe('Symphony batch delete API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('submits selected issues and requests cancellation of active tasks', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        status: 'deleted',
        requested_count: 2,
        deleted_count: 2,
        failed_count: 0,
        results: [
          { identifier: 'LOCAL-ONE', status: 'deleted' },
          { identifier: 'LOCAL-TWO', status: 'deleted' },
        ],
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await batchDeleteSymphonyLocalIssues([
      { id: 'issue-one', identifier: 'LOCAL-ONE' },
      { id: 'issue-two', identifier: 'LOCAL-TWO' },
    ]);

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/symphony/local-issues/batch-delete',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const request = fetchMock.mock.calls[0][1];
    expect(JSON.parse(String(request.body))).toEqual({
      issues: [
        { id: 'issue-one', identifier: 'LOCAL-ONE' },
        { id: 'issue-two', identifier: 'LOCAL-TWO' },
      ],
      cancel_active: true,
    });
    expect(result.deleted_count).toBe(2);
  });

  it('surfaces a top-level batch failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: 'at least one issue is required' }),
    }));

    await expect(batchDeleteSymphonyLocalIssues([])).rejects.toThrow(
      'at least one issue is required',
    );
  });
});
