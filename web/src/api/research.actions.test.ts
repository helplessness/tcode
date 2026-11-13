import { afterEach, describe, expect, it, vi } from 'vitest';

import { buildResearchPack, confirmResearchCandidate, getResearchCenter, listResearchPackImports, removeResearchPackListing } from './research';


afterEach(() => {
  vi.unstubAllGlobals();
});

const response = (payload: Record<string, any>) => ({
  ok: true,
  json: async () => payload,
});

const installBrowserGlobals = (fetchMock: ReturnType<typeof vi.fn>) => {
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('window', {
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
  });
};

describe('research natural-language actions', () => {
  it('loads the workspace research-center snapshot', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      current_project: null,
      candidates: [],
      history: [],
      candidate_limit: 3,
      project_limit: 1,
    }));
    installBrowserGlobals(fetchMock);

    const result = await getResearchCenter('workspace-1');

    expect(fetchMock.mock.calls[0][0]).toBe('/api/research/center?workspace_id=workspace-1');
    expect(result.project_limit).toBe(1);
  });

  it('submits the reviewed candidate digest when confirming a formal topic', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      candidate: { candidate_id: 'candidate-1' },
      project: { research_id: 'research-1' },
    }));
    installBrowserGlobals(fetchMock);

    await confirmResearchCandidate('candidate-1', 'workspace-1', 'sha256:reviewed');

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/research/candidates/candidate-1/confirm?workspace_id=workspace-1');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ expected_candidate_digest: 'sha256:reviewed' });
  });

  it('keeps ResearchPack source-digest and privacy acknowledgement on card builds', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      export: { pack_id: 'pack-1', archive_name: 'pack-1.researchpack.zip' },
    }));
    installBrowserGlobals(fetchMock);

    await buildResearchPack(
      'research-1',
      {
        privacy_profile: 'strict',
        expected_source_digest: 'sha256:source',
        acknowledge_privacy: true,
      },
      'workspace-1',
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/research/packs/build?workspace_id=workspace-1');
    expect(JSON.parse(init.body)).toMatchObject({
      mode: 'project',
      research_ids: ['research-1'],
      privacy_profile: 'strict',
      expected_source_digest: 'sha256:source',
      acknowledge_privacy: true,
      attribution_mode: 'anonymous',
    });
  });
});


describe('research listing management', () => {
  it('loads the requested page and preserves its cursor', async () => {
    const mock = vi.fn().mockResolvedValue(response({ imports: [], next_offset: 100 }));
    installBrowserGlobals(mock);
    expect((await listResearchPackImports('workspace-1', true, 50)).next_offset).toBe(100);
    expect(mock.mock.calls[0][0]).toBe('/api/research/packs/imports?workspace_id=workspace-1&hidden=true&offset=50');
  });
  it('removes and restores the same owner-scoped asset', async () => {
    const mock = vi.fn().mockResolvedValue(response({ removed: true, id: 'asset-1' }));
    installBrowserGlobals(mock);
    await removeResearchPackListing('asset-1', 'asset', false, 'workspace-1');
    await removeResearchPackListing('asset-1', 'asset', true, 'workspace-1');
    expect(mock.mock.calls.map(([url, init]) => [url, init.method])).toEqual([
      ['/api/research/pack-assets/asset-1?workspace_id=workspace-1', 'DELETE'],
      ['/api/research/pack-assets/asset-1/restore?workspace_id=workspace-1', 'POST'],
    ]);
  });
});
