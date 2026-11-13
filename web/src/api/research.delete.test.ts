import { afterEach, describe, expect, it, vi } from 'vitest';

import { deleteResearchProject } from './research';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('research project permanent deletion', () => {
  it('uses owner-scoped DELETE with an explicit yes confirmation', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        status: 'deleted',
        deletion: {
          deletion_id: 'project-delete-1',
          research_id: 'research-1',
          records_deleted: {},
          private_session_event_count: 1,
          reassigned_event_count: 0,
          local_files_removed: 2,
          local_files_preserved: 0,
          imported_papers_removed: 0,
          imported_wiki_pages_removed: 0,
          evidence_records_removed: 0,
          cleanup_error_count: 0,
        },
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('window', {
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
    });

    const result = await deleteResearchProject('research-1', 'yes', 'preview-1', 'workspace-1');

    expect(result.status).toBe('deleted');
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/research/projects/research-1?workspace_id=workspace-1');
    expect(init.method).toBe('DELETE');
    expect(JSON.parse(init.body)).toEqual({ confirmation: 'yes', preview_token: 'preview-1' });
  });
});
