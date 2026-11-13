import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  cancelProteinRunAttempt,
  deleteProteinRun,
  getProteinRun,
  listProteinRuns,
  proteinRunArtifactUrl,
  reconcileProteinRunAttempt,
  retryProteinRunAttempt,
} from './proteinRuns';

describe('protein runs API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('keeps list and detail requests inside the active workspace scope', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ workspace_id: 'workspace-2', count: 0, total: 0, runs: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run: { run_id: 'run-one' } }),
      });
    vi.stubGlobal('fetch', fetchMock);

    await listProteinRuns('workspace-2');
    await getProteinRun('run-one', 'workspace-2');

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/protein-runs?workspace_id=workspace-2');
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/protein-runs/run-one?workspace_id=workspace-2');
  });

  it('scopes artifact links and surfaces API failures', async () => {
    expect(proteinRunArtifactUrl('/api/protein-runs/run-one/artifacts/outputs/a.pdb', 'workspace-2'))
      .toBe('/api/protein-runs/run-one/artifacts/outputs/a.pdb?workspace_id=workspace-2');

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'protein run not found' }),
    }));
    await expect(getProteinRun('missing', 'workspace-2')).rejects.toThrow('protein run not found');
  });

  it('uses the Run-aware action endpoints with one idempotency key per request', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: 'ok', run_id: 'run-one', action: 'retry' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await retryProteinRunAttempt('run-one', 'attempt-one', 'workspace-2');
    await cancelProteinRunAttempt('run-one', 'attempt-two', 'workspace-2');

    expect(fetchMock.mock.calls[0][0]).toBe('/api/protein-runs/run-one/attempts/attempt-one/retry?workspace_id=workspace-2');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/protein-runs/run-one/attempts/attempt-two/cancel?workspace_id=workspace-2');
    for (const [, init] of fetchMock.mock.calls) {
      expect(init.method).toBe('POST');
      const body = JSON.parse(String(init.body || '{}'));
      expect(body.idempotency_key).toMatch(/^run-action-|^[0-9a-f-]{20,}$/i);
    }
  });

  it('uses the owner-scoped manual reconcile endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: 'succeeded', run_id: 'run-one', attempt_id: 'attempt-one', action: 'reconcile' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await reconcileProteinRunAttempt('run-one', 'attempt-one', 'workspace-2');

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/protein-runs/run-one/attempts/attempt-one/reconcile?workspace_id=workspace-2',
      { method: 'POST' },
    );
  });

  it('deletes a Run inside the active workspace scope', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        status: 'deleted',
        run_id: 'run-one',
        removed_output_count: 2,
        removed_attempt_count: 1,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await deleteProteinRun('run-one', 'workspace-2');

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/protein-runs/run-one?workspace_id=workspace-2',
      { method: 'DELETE' },
    );
  });
});
